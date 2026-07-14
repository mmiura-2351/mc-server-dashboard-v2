package game

import (
	"bufio"
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/mc"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/metrics"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/tunnel"
)

func nopLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// seriesValue returns the value of the counter or gauge series named name whose
// labels match the given pairs, or 0 if absent.
func seriesValue(t *testing.T, reg *prometheus.Registry, name string, labels map[string]string) float64 {
	t.Helper()
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() != name {
			continue
		}
		for _, m := range f.GetMetric() {
			if !labelsMatch(m.GetLabel(), labels) {
				continue
			}
			switch {
			case m.Counter != nil:
				return m.GetCounter().GetValue()
			case m.Gauge != nil:
				return m.GetGauge().GetValue()
			}
		}
	}
	return 0
}

func labelsMatch(pairs []*dto.LabelPair, want map[string]string) bool {
	if len(pairs) != len(want) {
		return false
	}
	for _, p := range pairs {
		if want[p.GetName()] != p.GetValue() {
			return false
		}
	}
	return true
}

// waitSeries polls until the named series reaches want or the deadline elapses.
func waitSeries(t *testing.T, reg *prometheus.Registry, name string, labels map[string]string, want float64, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for {
		if got := seriesValue(t, reg, name, labels); got == want {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s%v did not reach %v within %v (last=%v)", name, labels, want, timeout, seriesValue(t, reg, name, labels))
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// loginHandshake builds a parsed login (next_state=2) handshake for slug under
// baseDomain.
func loginHandshake(t *testing.T, slug, baseDomain string) mc.Handshake {
	t.Helper()
	raw := handshakePacket(765, slug+"."+baseDomain, 25565, 2)
	hs, err := mc.ReadHandshake(bufio.NewReaderSize(strings.NewReader(string(raw)), mc.MaxPreRouteBytes))
	if err != nil {
		t.Fatalf("parse handshake: %v", err)
	}
	return hs
}

// TestGameHandshakeInvalidDropIncrementsMetric asserts a connection that never
// sends a parseable handshake increments relay_game_drops_total{handshake_invalid}.
func TestGameHandshakeInvalidDropIncrementsMetric(t *testing.T) {
	reg := prometheus.NewRegistry()
	l := &Listener{
		resolver: &fakeResolver{domain: "mc.example.com"},
		caps:     ipcaps.NewIPCaps(32, 10, 0, time.Now, nil),
		metrics:  metrics.New(reg, "test"),
		logger:   nopLogger(),
	}

	playerSide, relaySide := net.Pipe()
	go func() { _ = playerSide.Close() }() // immediate EOF ⇒ handshake read fails

	l.handle(context.Background(), relaySide)

	if got := seriesValue(t, reg, "relay_game_drops_total", map[string]string{"reason": metrics.DropHandshakeInvalid}); got != 1 {
		t.Errorf("drops{handshake_invalid} = %v, want 1", got)
	}
}

// TestGameUnknownHostDropIncrementsMetric asserts a handshake for a hostname
// outside the base domain increments relay_game_drops_total{unknown_host}.
func TestGameUnknownHostDropIncrementsMetric(t *testing.T) {
	reg := prometheus.NewRegistry()
	l := &Listener{
		resolver: &fakeResolver{domain: "mc.example.com"},
		caps:     ipcaps.NewIPCaps(32, 10, 0, time.Now, nil),
		metrics:  metrics.New(reg, "test"),
		logger:   nopLogger(),
	}

	playerSide, relaySide := net.Pipe()
	go func() {
		_ = playerSide.SetWriteDeadline(time.Now().Add(time.Second))
		_, _ = playerSide.Write(handshakePacket(765, "wronghost.example.net", 25565, 1))
		_ = playerSide.Close()
	}()

	l.handle(context.Background(), relaySide)

	if got := seriesValue(t, reg, "relay_game_drops_total", map[string]string{"reason": metrics.DropUnknownHost}); got != 1 {
		t.Errorf("drops{unknown_host} = %v, want 1", got)
	}
}

// TestGameLoginNotFoundDropIncrementsMetric asserts a login whose slug resolves
// to NOT_FOUND increments relay_game_drops_total{not_found}.
func TestGameLoginNotFoundDropIncrementsMetric(t *testing.T) {
	reg := prometheus.NewRegistry()
	l := &Listener{
		resolver: &fakeResolver{result: apiclient.ResolveResult{Decision: apiclient.DecisionNotFound}, domain: "mc.example.com"},
		caps:     ipcaps.NewIPCaps(32, 100, 0, time.Now, nil),
		metrics:  metrics.New(reg, "test"),
		logger:   nopLogger(),
	}

	hs := loginHandshake(t, "amber", "mc.example.com")
	r := bufio.NewReaderSize(strings.NewReader(string(loginStartPacket("Steve"))), mc.MaxPreRouteBytes)
	playerSide, relaySide := net.Pipe()
	defer func() { _ = playerSide.Close() }()

	l.handleLogin(context.Background(), relaySide, r, hs, "amber", "1.2.3.4")

	if got := seriesValue(t, reg, "relay_game_drops_total", map[string]string{"reason": metrics.DropNotFound}); got != 1 {
		t.Errorf("drops{not_found} = %v, want 1", got)
	}
}

// TestGameLoginResolveUnavailableDropIncrementsMetric asserts a login whose
// ResolveJoin errors increments relay_game_drops_total{resolve_unavailable}.
func TestGameLoginResolveUnavailableDropIncrementsMetric(t *testing.T) {
	reg := prometheus.NewRegistry()
	l := &Listener{
		resolver: &fakeResolver{err: errors.New("api down"), domain: "mc.example.com"},
		caps:     ipcaps.NewIPCaps(32, 100, 0, time.Now, nil),
		metrics:  metrics.New(reg, "test"),
		logger:   nopLogger(),
	}

	hs := loginHandshake(t, "amber", "mc.example.com")
	r := bufio.NewReaderSize(strings.NewReader(string(loginStartPacket("Steve"))), mc.MaxPreRouteBytes)
	playerSide, relaySide := net.Pipe()
	defer func() { _ = playerSide.Close() }()

	done := make(chan struct{})
	go func() {
		l.handleLogin(context.Background(), relaySide, r, hs, "amber", "1.2.3.4")
		close(done)
	}()
	// Drain the Login Disconnect the resolve-unavailable path writes.
	_ = playerSide.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, _ = playerSide.Read(make([]byte, 256))
	<-done

	if got := seriesValue(t, reg, "relay_game_drops_total", map[string]string{"reason": metrics.DropResolveUnavailable}); got != 1 {
		t.Errorf("drops{resolve_unavailable} = %v, want 1", got)
	}
}

// TestGameLoginRateCapRejectionIncrementsMetric asserts a login from an IP that
// has exhausted its join-rate budget increments
// relay_ipcaps_rejections_total{listener="game",kind="rate"}.
func TestGameLoginRateCapRejectionIncrementsMetric(t *testing.T) {
	reg := prometheus.NewRegistry()
	caps := ipcaps.NewIPCaps(32, 1, 0, time.Now, nil) // 1 join/s
	l := &Listener{
		resolver: &fakeResolver{domain: "mc.example.com"},
		caps:     caps,
		metrics:  metrics.New(reg, "test"),
		logger:   nopLogger(),
	}
	if !caps.AllowJoin("1.2.3.4") {
		t.Fatal("first AllowJoin should succeed")
	}

	hs := loginHandshake(t, "amber", "mc.example.com")
	r := bufio.NewReaderSize(strings.NewReader(string(loginStartPacket("Steve"))), mc.MaxPreRouteBytes)
	playerSide, relaySide := net.Pipe()
	defer func() { _ = playerSide.Close() }()

	l.handleLogin(context.Background(), relaySide, r, hs, "amber", "1.2.3.4")

	if got := seriesValue(t, reg, "relay_ipcaps_rejections_total",
		map[string]string{"listener": metrics.ListenerGame, "kind": metrics.CapKindRate}); got != 1 {
		t.Errorf("ipcaps_rejections{game,rate} = %v, want 1", got)
	}
}

// TestGameLoginSpliceSessionMetrics asserts a login that splices increments
// relay_game_sessions_accepted_total and holds relay_game_active_sessions at +1
// for the splice, releasing it to 0 when the splice ends.
func TestGameLoginSpliceSessionMetrics(t *testing.T) {
	reg := prometheus.NewRegistry()
	tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
	l := &Listener{
		tokens:   tokens,
		sessions: &fakeSessionRecorder{},
		metrics:  metrics.New(reg, "test"),
		logger:   nopLogger(),
	}

	hs := mc.Handshake{Raw: handshakePacket(765, "amber.mc.example.com", 25565, 2)}
	login := mc.LoginStart{Name: "Steve", Raw: loginStartPacket("Steve")}

	playerSide, relaySide := net.Pipe()  // conn (player)
	workerSide, tunnelSide := net.Pipe() // tconn (worker dial-back)
	defer func() { _ = playerSide.Close() }()

	// Deliver the worker dial-back once spliceLogin has registered its waiter.
	go func() {
		time.Sleep(50 * time.Millisecond)
		tokens.Deliver("tok", tunnelSide)
	}()

	// Worker side: read the OK ack + replayed handshake + login, then hold the
	// connection open so the splice stays active until released.
	releaseWorker := make(chan struct{})
	go func() {
		replayed := make([]byte, 3+len(hs.Raw)+len(login.Raw))
		_, _ = io.ReadFull(workerSide, replayed)
		<-releaseWorker
		_ = workerSide.Close()
	}()

	spliceDone := make(chan struct{})
	go func() {
		l.spliceLogin(context.Background(), relaySide, bufio.NewReaderSize(relaySide, mc.MaxPreRouteBytes),
			hs, login, "srv", "amber", "1.2.3.4", "tok")
		close(spliceDone)
	}()

	// The splice is active: active_sessions holds at 1 and accepted is 1.
	waitSeries(t, reg, "relay_game_active_sessions", nil, 1, 2*time.Second)
	if got := seriesValue(t, reg, "relay_game_sessions_accepted_total", nil); got != 1 {
		t.Errorf("sessions_accepted = %v, want 1", got)
	}

	// End the splice → active_sessions returns to 0.
	close(releaseWorker)
	select {
	case <-spliceDone:
	case <-time.After(2 * time.Second):
		t.Fatal("spliceLogin did not return after the worker closed")
	}
	waitSeries(t, reg, "relay_game_active_sessions", nil, 0, 2*time.Second)
}
