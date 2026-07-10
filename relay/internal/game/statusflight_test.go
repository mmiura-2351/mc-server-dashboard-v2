package game

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/mc"
)

// blockingResolver is a Resolver whose ResolveJoin signals each call on
// started, then blocks until release is closed. It lets tests hold a status
// flight open while concurrent callers pile up behind it.
type blockingResolver struct {
	started chan struct{} // receives one send per ResolveJoin call
	release chan struct{} // ResolveJoin blocks until this closes
	result  apiclient.ResolveResult
	err     error

	mu    sync.Mutex
	calls int
}

func (b *blockingResolver) ResolveJoin(_ context.Context, _, _ string, _ apiclient.Intent) (apiclient.ResolveResult, error) {
	b.mu.Lock()
	b.calls++
	b.mu.Unlock()
	b.started <- struct{}{}
	<-b.release
	return b.result, b.err
}

func (b *blockingResolver) BaseDomain() string { return "mc.example.com" }

func (b *blockingResolver) callCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.calls
}

// statusFlightListener builds a Listener wired for coalesceStatus tests.
func statusFlightListener(resolver Resolver) *Listener {
	return &Listener{
		resolver: resolver,
		cache:    NewStatusCache(5*time.Second, 1024, time.Now),
		logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

// TestCoalesceStatusConcurrentMissesSingleExchange proves that concurrent
// cache misses for the same slug run exactly one underlying exchange and all
// callers receive its result.
func TestCoalesceStatusConcurrentMissesSingleExchange(t *testing.T) {
	resolver := &blockingResolver{
		started: make(chan struct{}, 1),
		release: make(chan struct{}),
		result:  apiclient.ResolveResult{Decision: apiclient.DecisionStopped, DisplayName: "X"},
	}
	l := statusFlightListener(resolver)

	_, hs := statusHandshake(t, "amber", "mc.example.com")

	const waiters = 3
	results := make(chan string, 1+waiters)

	// Leader: enters ResolveJoin and blocks on release.
	go func() { results <- l.coalesceStatus(context.Background(), hs, "amber", "10.0.0.1") }()
	<-resolver.started

	// Waiters join while the leader is held inside the resolver, so the flight
	// entry is guaranteed to still be present.
	for i := 0; i < waiters; i++ {
		go func() { results <- l.coalesceStatus(context.Background(), hs, "amber", "10.0.0.2") }()
	}
	// Give the waiter goroutines time to reach the flight join (the codebase's
	// standard pattern for "let the goroutine reach its blocking point").
	time.Sleep(50 * time.Millisecond)
	close(resolver.release)

	want := mc.SynthesizedStatus(mc.StoppedMOTD("X"))
	for i := 0; i < 1+waiters; i++ {
		select {
		case got := <-results:
			if got != want {
				t.Errorf("caller %d: got %q, want %q", i, got, want)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("caller did not receive a coalesced result")
		}
	}

	if c := resolver.callCount(); c != 1 {
		t.Errorf("ResolveJoin should run once for coalesced misses, got %d calls", c)
	}
}

// TestCoalesceStatusLeaderErrorPropagates proves a failing leader exchange
// propagates its fallback to every waiter and caches nothing.
func TestCoalesceStatusLeaderErrorPropagates(t *testing.T) {
	resolver := &blockingResolver{
		started: make(chan struct{}, 1),
		release: make(chan struct{}),
		err:     errors.New("connection refused"),
	}
	l := statusFlightListener(resolver)

	_, hs := statusHandshake(t, "amber", "mc.example.com")

	results := make(chan string, 2)
	go func() { results <- l.coalesceStatus(context.Background(), hs, "amber", "10.0.0.1") }()
	<-resolver.started
	go func() { results <- l.coalesceStatus(context.Background(), hs, "amber", "10.0.0.2") }()
	time.Sleep(50 * time.Millisecond)
	close(resolver.release)

	want := mc.SynthesizedStatus(mc.UnavailableMOTD)
	for i := 0; i < 2; i++ {
		select {
		case got := <-results:
			if got != want {
				t.Errorf("caller %d: got %q, want %q", i, got, want)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("caller did not receive the leader's error result")
		}
	}

	if _, ok := l.cache.Get("amber"); ok {
		t.Error("a failed exchange must not populate the cache")
	}
	if c := resolver.callCount(); c != 1 {
		t.Errorf("ResolveJoin should run once, got %d calls", c)
	}
}

// TestCoalesceStatusDifferentSlugsDoNotSerialize proves flights for distinct
// slugs run concurrently: the second slug's exchange starts while the first
// is still in flight.
func TestCoalesceStatusDifferentSlugsDoNotSerialize(t *testing.T) {
	resolver := &blockingResolver{
		started: make(chan struct{}, 2),
		release: make(chan struct{}),
		result:  apiclient.ResolveResult{Decision: apiclient.DecisionStopped, DisplayName: "X"},
	}
	l := statusFlightListener(resolver)

	_, hsAmber := statusHandshake(t, "amber", "mc.example.com")
	_, hsCoral := statusHandshake(t, "coral", "mc.example.com")

	results := make(chan string, 2)
	go func() { results <- l.coalesceStatus(context.Background(), hsAmber, "amber", "10.0.0.1") }()
	go func() { results <- l.coalesceStatus(context.Background(), hsCoral, "coral", "10.0.0.1") }()

	// Both exchanges must be in flight at once; if flights serialized across
	// slugs, the second started signal would never arrive before release.
	for i := 0; i < 2; i++ {
		select {
		case <-resolver.started:
		case <-time.After(2 * time.Second):
			t.Fatal("second slug's exchange did not start while the first was in flight")
		}
	}
	close(resolver.release)

	for i := 0; i < 2; i++ {
		select {
		case <-results:
		case <-time.After(2 * time.Second):
			t.Fatal("caller did not receive a result")
		}
	}
}

// TestWaitStatusFlightTimeout proves a waiter whose wait times out gives up
// cleanly — answering unavailable — without cancelling the leader's flight or
// poisoning its result for other waiters.
func TestWaitStatusFlightTimeout(t *testing.T) {
	l := &Listener{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}

	f, leader := l.flights.join("amber")
	if !leader {
		t.Fatal("first join must lead the flight")
	}

	// A waiter with a tiny timeout gives up while the flight is still open.
	if got, want := l.waitStatusFlight(context.Background(), f, 10*time.Millisecond), mc.SynthesizedStatus(mc.UnavailableMOTD); got != want {
		t.Errorf("timed-out waiter: got %q, want %q", got, want)
	}

	// A second waiter is still served once the leader finishes: the timed-out
	// waiter neither cancelled the flight nor poisoned its result.
	const wantJSON = `{"description":{"text":"late"}}`
	done := make(chan string, 1)
	go func() { done <- l.waitStatusFlight(context.Background(), f, 2*time.Second) }()
	l.flights.finish("amber", f, wantJSON)

	select {
	case got := <-done:
		if got != wantJSON {
			t.Errorf("patient waiter: got %q, want %q", got, wantJSON)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("patient waiter did not receive the leader's result")
	}
}

// TestWaitStatusFlightContextCancelled proves a waiter drops (empty result)
// when the listener context is cancelled (shutdown) mid-wait.
func TestWaitStatusFlightContextCancelled(t *testing.T) {
	l := &Listener{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}

	f, _ := l.flights.join("amber")

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan string, 1)
	go func() { done <- l.waitStatusFlight(ctx, f, 2*time.Second) }()
	cancel()

	select {
	case got := <-done:
		if got != "" {
			t.Errorf("cancelled waiter should drop, got %q", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("cancelled waiter did not return")
	}
}

// TestStatusFlightsJoinFinish pins the flight-group lifecycle: the first join
// leads, later joins wait on the same flight, and finish retires the entry so
// the next join leads a fresh flight.
func TestStatusFlightsJoinFinish(t *testing.T) {
	var g statusFlights

	f1, leader := g.join("amber")
	if !leader {
		t.Fatal("first join must lead")
	}
	f2, leader := g.join("amber")
	if leader {
		t.Fatal("second join must wait, not lead")
	}
	if f1 != f2 {
		t.Fatal("waiter must share the leader's flight")
	}

	g.finish("amber", f1, `{"a":1}`)
	select {
	case <-f1.done:
	default:
		t.Fatal("finish must release waiters")
	}
	if f1.json != `{"a":1}` {
		t.Errorf("flight result = %q, want %q", f1.json, `{"a":1}`)
	}

	if _, leader := g.join("amber"); !leader {
		t.Fatal("join after finish must lead a fresh flight")
	}
}

// TestCoalesceStatusLeaderRechecksCache proves a goroutine that missed the
// cache but leads a fresh flight re-checks the cache before paying for an
// exchange (another flight may have completed in between).
func TestCoalesceStatusLeaderRechecksCache(t *testing.T) {
	const cachedJSON = `{"description":{"text":"cached"}}`
	resolver := &blockingResolver{
		started: make(chan struct{}, 1),
		release: make(chan struct{}),
	}
	l := statusFlightListener(resolver)
	l.cache.Put("amber", cachedJSON)

	_, hs := statusHandshake(t, "amber", "mc.example.com")

	if got := l.coalesceStatus(context.Background(), hs, "amber", "10.0.0.1"); got != cachedJSON {
		t.Errorf("got %q, want cached %q", got, cachedJSON)
	}
	if c := resolver.callCount(); c != 0 {
		t.Errorf("ResolveJoin should not run when the cache filled in between, got %d calls", c)
	}
}
