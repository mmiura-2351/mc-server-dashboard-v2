package relaysvc

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc/connectivity"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
)

type fakeRegistrar struct {
	mu         sync.Mutex
	baseDomain string
	regErr     error
	regCalls   int
	lastActive []string
	// regFn, when set, drives Register: it receives the call's context and
	// returns the result, overriding baseDomain/regErr. Lets tests model a
	// black-holed call (block on ctx) or a fail-then-succeed sequence.
	regFn func(ctx context.Context) (string, error)
}

func (f *fakeRegistrar) Register(ctx context.Context, _, _ string, active []string) (string, error) {
	f.mu.Lock()
	f.regCalls++
	f.lastActive = active
	fn := f.regFn
	regErr := f.regErr
	baseDomain := f.baseDomain
	f.mu.Unlock()

	if fn != nil {
		return fn(ctx)
	}
	if regErr != nil {
		return "", regErr
	}
	return baseDomain, nil
}

func (f *fakeRegistrar) calls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.regCalls
}

func (f *fakeRegistrar) ResolveJoin(_ context.Context, _, _ string, _ apiclient.Intent) (apiclient.ResolveResult, error) {
	return apiclient.ResolveResult{Decision: apiclient.DecisionTunnel, Token: "tok"}, nil
}

type fakeSessions struct{ ids []string }

func (f fakeSessions) SnapshotActive() ([]string, func()) { return f.ids, func() {} }

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestRegisterLearnsBaseDomain(t *testing.T) {
	reg := &fakeRegistrar{baseDomain: "mc.example.com"}
	svc := New(reg, nil, fakeSessions{ids: []string{"s1"}}, "relay:25665", "CA", discardLogger())

	if svc.BaseDomain() != "" {
		t.Error("base_domain should be empty before Register")
	}
	if err := svc.RegisterOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if svc.BaseDomain() != "mc.example.com" {
		t.Errorf("base_domain = %q after Register", svc.BaseDomain())
	}
	if len(reg.lastActive) != 1 || reg.lastActive[0] != "s1" {
		t.Errorf("Register did not carry active session ids: %v", reg.lastActive)
	}
}

func TestRegisterOnceError(t *testing.T) {
	reg := &fakeRegistrar{regErr: errors.New("down")}
	svc := New(reg, nil, fakeSessions{}, "relay:25665", "", discardLogger())
	if err := svc.RegisterOnce(context.Background()); err == nil {
		t.Error("RegisterOnce should surface the API error")
	}
	if svc.BaseDomain() != "" {
		t.Error("a failed Register must not set base_domain")
	}
}

// TestRunReRegistersPeriodically asserts Run does not register once and block:
// after a success it re-registers every reRegisterInterval so an API restart
// heals (and active session ids are re-delivered for orphan healing) without a
// relay restart.
func TestRunReRegistersPeriodically(t *testing.T) {
	prev := reRegisterInterval
	reRegisterInterval = time.Millisecond
	defer func() { reRegisterInterval = prev }()

	reg := &fakeRegistrar{baseDomain: "mc.example.com"}
	svc := New(reg, nil, fakeSessions{ids: []string{"s1"}}, "relay:25665", "CA", discardLogger())

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { svc.Run(ctx); close(done) }()

	deadline := time.After(2 * time.Second)
	for reg.calls() < 3 {
		select {
		case <-deadline:
			t.Fatalf("Run re-registered only %d times; expected periodic re-registration", reg.calls())
		default:
			time.Sleep(time.Millisecond)
		}
	}
	cancel()
	<-done
}

func TestResolveJoinProxies(t *testing.T) {
	svc := New(&fakeRegistrar{}, nil, fakeSessions{}, "", "", discardLogger())
	res, err := svc.ResolveJoin(context.Background(), "amber", "1.2.3.4", apiclient.IntentLogin)
	if err != nil {
		t.Fatal(err)
	}
	if res.Decision != apiclient.DecisionTunnel || res.Token != "tok" {
		t.Errorf("ResolveJoin proxy returned %+v", res)
	}
}

// fakeConn models a gRPC client connection's connectivity state with explicit
// transitions, so Run's recover-then-re-register path (issue #987) is testable
// without a live API. setState wakes any waiter in WaitForStateChange.
type fakeConn struct {
	mu         sync.Mutex
	cond       *sync.Cond
	state      connectivity.State
	connectCnt int
}

func newFakeConn(s connectivity.State) *fakeConn {
	c := &fakeConn{state: s}
	c.cond = sync.NewCond(&c.mu)
	return c
}

func (c *fakeConn) GetState() connectivity.State {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.state
}

func (c *fakeConn) setState(s connectivity.State) {
	c.mu.Lock()
	c.state = s
	c.mu.Unlock()
	c.cond.Broadcast()
}

func (c *fakeConn) Connect() {
	c.mu.Lock()
	c.connectCnt++
	c.mu.Unlock()
}

func (c *fakeConn) connects() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.connectCnt
}

func (c *fakeConn) WaitForStateChange(ctx context.Context, source connectivity.State) bool {
	done := make(chan struct{})
	go func() {
		<-ctx.Done()
		c.cond.Broadcast()
		close(done)
	}()
	defer func() { c.cond.Broadcast(); <-done }()

	c.mu.Lock()
	defer c.mu.Unlock()
	for c.state == source {
		if ctx.Err() != nil {
			return false
		}
		c.cond.Wait()
	}
	return true
}

// TestRunReRegistersPromptlyOnReconnect proves the issue #987 fix: when the API
// connection drops (Register fails, conn non-Ready) and later recovers to Ready,
// Run re-registers within seconds — well under the periodic backstop interval —
// instead of waiting up to a full interval.
func TestRunReRegistersPromptlyOnReconnect(t *testing.T) {
	// Make the periodic backstop and backoff effectively infinite so the only
	// path to a prompt re-register is the conn-Ready short-circuit.
	prevInterval, prevBackoff := reRegisterInterval, backoffMax
	reRegisterInterval = time.Hour
	backoffMax = time.Hour
	defer func() { reRegisterInterval, backoffMax = prevInterval, prevBackoff }()

	conn := newFakeConn(connectivity.TransientFailure)
	succeeded := make(chan struct{})
	reg := &fakeRegistrar{
		regFn: func(_ context.Context) (string, error) {
			// First call fails (API is down); once the conn is Ready, succeed.
			if conn.GetState() != connectivity.Ready {
				return "", errors.New("api down")
			}
			select {
			case <-succeeded:
			default:
				close(succeeded)
			}
			return "mc.example.com", nil
		},
	}
	svc := New(reg, conn, fakeSessions{}, "relay:25665", "CA", discardLogger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { svc.Run(ctx); close(done) }()

	// Let the first Register fail and the loop park in waitRetry.
	time.Sleep(50 * time.Millisecond)

	// API comes back: connection recovers to Ready.
	recoveredAt := time.Now()
	conn.setState(connectivity.Ready)

	select {
	case <-succeeded:
		if d := time.Since(recoveredAt); d > 2*time.Second {
			t.Fatalf("re-register took %v after recovery; expected prompt (well under the %v backstop)", d, reRegisterInterval)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run did not re-register within 3s of the connection recovering")
	}

	if conn.connects() == 0 {
		t.Error("waitRetry should nudge the connection via Connect()")
	}
	cancel()
	<-done
}

// TestRegisterOnceRespectsDeadline proves the issue #971 fix: a black-holed
// Register call returns within registerTimeout rather than hanging.
func TestRegisterOnceRespectsDeadline(t *testing.T) {
	prev := registerTimeout
	registerTimeout = 100 * time.Millisecond
	defer func() { registerTimeout = prev }()

	reg := &fakeRegistrar{
		regFn: func(ctx context.Context) (string, error) {
			<-ctx.Done() // black hole: block until the call's deadline fires.
			return "", ctx.Err()
		},
	}
	svc := New(reg, nil, fakeSessions{}, "relay:25665", "CA", discardLogger())

	start := time.Now()
	err := svc.RegisterOnce(context.Background())
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("RegisterOnce should surface the deadline error")
	}
	if elapsed > 500*time.Millisecond {
		t.Fatalf("RegisterOnce took %v; expected it to honour the %v deadline", elapsed, registerTimeout)
	}
}

// trackingSessions records the order of SnapshotActive acquire/release relative
// to Register calls, so we can assert the barrier is held across the RPC.
type trackingSessions struct {
	mu     sync.Mutex
	ids    []string
	events []string // "snapshot", "release", interleaved with registrar events
}

func (ts *trackingSessions) SnapshotActive() ([]string, func()) {
	ts.mu.Lock()
	ts.events = append(ts.events, "snapshot")
	ids := ts.ids
	ts.mu.Unlock()
	return ids, func() {
		ts.mu.Lock()
		ts.events = append(ts.events, "release")
		ts.mu.Unlock()
	}
}

func (ts *trackingSessions) getEvents() []string {
	ts.mu.Lock()
	defer ts.mu.Unlock()
	out := make([]string, len(ts.events))
	copy(out, ts.events)
	return out
}

// trackingRegistrar records a "register" event so ordering can be verified.
type trackingRegistrar struct {
	sessions *trackingSessions
	base     string
}

func (tr *trackingRegistrar) Register(_ context.Context, _, _ string, _ []string) (string, error) {
	tr.sessions.mu.Lock()
	tr.sessions.events = append(tr.sessions.events, "register")
	tr.sessions.mu.Unlock()
	return tr.base, nil
}

func (tr *trackingRegistrar) ResolveJoin(_ context.Context, _, _ string, _ apiclient.Intent) (apiclient.ResolveResult, error) {
	return apiclient.ResolveResult{}, nil
}

// TestRegisterOnceBarrierOrdering asserts that RegisterOnce acquires the barrier
// (SnapshotActive) before the Register RPC and releases it after.
func TestRegisterOnceBarrierOrdering(t *testing.T) {
	ts := &trackingSessions{ids: []string{"s1"}}
	reg := &trackingRegistrar{sessions: ts, base: "mc.example.com"}
	svc := New(reg, nil, ts, "relay:25665", "CA", discardLogger())

	if err := svc.RegisterOnce(context.Background()); err != nil {
		t.Fatal(err)
	}

	events := ts.getEvents()
	if len(events) != 3 {
		t.Fatalf("events = %v, want [snapshot register release]", events)
	}
	if events[0] != "snapshot" || events[1] != "register" || events[2] != "release" {
		t.Errorf("ordering = %v, want [snapshot register release]", events)
	}
}

// TestWaitRetryStopsOnShutdown asserts waitRetry returns false (stop the loop)
// when ctx is cancelled while the connection is down, rather than treating it as
// a backoff timeout.
func TestWaitRetryStopsOnShutdown(t *testing.T) {
	prev := backoffMax
	backoffMax = time.Hour
	defer func() { backoffMax = prev }()

	conn := newFakeConn(connectivity.TransientFailure)
	svc := New(&fakeRegistrar{}, conn, fakeSessions{}, "", "", discardLogger())

	ctx, cancel := context.WithCancel(context.Background())
	result := make(chan bool, 1)
	go func() { result <- svc.waitRetry(ctx, time.Hour) }()

	time.Sleep(20 * time.Millisecond)
	cancel()

	select {
	case proceed := <-result:
		if proceed {
			t.Error("waitRetry should return false on shutdown")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("waitRetry did not return after ctx cancel")
	}
}
