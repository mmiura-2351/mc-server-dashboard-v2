package tunnel

import (
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/metrics"
)

// newInstrumentedListener builds a handle-only Listener wired to reg's metrics.
func newInstrumentedListener(reg *prometheus.Registry, maxConns uint32) *Listener {
	return &Listener{
		tokens:  NewTokenTable(10*time.Second, time.Now),
		caps:    ipcaps.NewIPCaps(maxConns, 0, -1, time.Now, nil),
		metrics: metrics.New(reg, "test"),
		logger:  slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

// counterValue returns the value of the counter series named name whose labels
// match the given pairs, or 0 if absent.
func counterValue(t *testing.T, reg *prometheus.Registry, name string, labels map[string]string) float64 {
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
			if labelsMatch(m.GetLabel(), labels) {
				return m.GetCounter().GetValue()
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

// TestTunnelCapRejectionIncrementsMetric asserts an over-cap dial-back increments
// relay_ipcaps_rejections_total{listener="tunnel",kind="conn"}.
func TestTunnelCapRejectionIncrementsMetric(t *testing.T) {
	reg := prometheus.NewRegistry()
	const maxConns = 1
	l := newInstrumentedListener(reg, maxConns)

	// Saturate the IP's single slot, then present an over-cap connection.
	if !l.caps.Acquire("1.1.1.1") {
		t.Fatal("pre-saturating acquire should succeed")
	}
	over, overCli := newCapConn("1.1.1.1")
	defer func() { _ = overCli.Close() }()
	go l.handle(over)
	if !closedWithin(over, 2*time.Second) {
		t.Fatal("over-cap connection was not closed")
	}

	if got := counterValue(t, reg, "relay_ipcaps_rejections_total",
		map[string]string{"listener": metrics.ListenerTunnel, "kind": metrics.CapKindConn}); got != 1 {
		t.Errorf("ipcaps_rejections{tunnel,conn} = %v, want 1", got)
	}
	l.caps.Release("1.1.1.1")
}

// TestTunnelDialbackNoWaiterIncrementsMetric asserts a valid handshake whose
// token has no waiter increments relay_tunnel_dialbacks_total{result="no_waiter"}.
func TestTunnelDialbackNoWaiterIncrementsMetric(t *testing.T) {
	reg := prometheus.NewRegistry()
	l := newInstrumentedListener(reg, 0)

	srv, cli := newCapConn("2.2.2.2")
	done := make(chan struct{})
	go func() { l.handle(srv); close(done) }()
	writeValidHandshake(t, cli, "no-waiter-token")
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("handle did not return")
	}
	_ = cli.Close()

	if got := counterValue(t, reg, "relay_tunnel_dialbacks_total",
		map[string]string{"result": metrics.DialbackNoWaiter}); got != 1 {
		t.Errorf("dialbacks{no_waiter} = %v, want 1", got)
	}
}

// TestTunnelDialbackDeliveredIncrementsMetric asserts a handshake matching a
// registered waiter increments relay_tunnel_dialbacks_total{result="delivered"}.
func TestTunnelDialbackDeliveredIncrementsMetric(t *testing.T) {
	reg := prometheus.NewRegistry()
	l := newInstrumentedListener(reg, 0)

	const token = "live-token"
	ch := l.tokens.Register(token)

	srv, cli := newCapConn("3.3.3.3")
	done := make(chan struct{})
	go func() { l.handle(srv); close(done) }()
	writeValidHandshake(t, cli, token)
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("handle did not return")
	}
	// The delivered connection was handed to the waiter's channel.
	select {
	case <-ch:
	case <-time.After(time.Second):
		t.Fatal("delivered connection did not reach the waiter")
	}
	_ = cli.Close()

	if got := counterValue(t, reg, "relay_tunnel_dialbacks_total",
		map[string]string{"result": metrics.DialbackDelivered}); got != 1 {
		t.Errorf("dialbacks{delivered} = %v, want 1", got)
	}
}

// TestTunnelDialbackHandshakeInvalidIncrementsMetric asserts a malformed
// handshake increments relay_tunnel_dialbacks_total{result="handshake_invalid"}.
func TestTunnelDialbackHandshakeInvalidIncrementsMetric(t *testing.T) {
	reg := prometheus.NewRegistry()
	l := newInstrumentedListener(reg, 0)

	srv, cli := newCapConn("4.4.4.4")
	done := make(chan struct{})
	go func() { l.handle(srv); close(done) }()
	_ = cli.SetWriteDeadline(time.Now().Add(time.Second))
	_, _ = cli.Write([]byte("NOT-THE-PREFIX\n"))
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("handle did not return")
	}
	_ = cli.Close()

	if got := counterValue(t, reg, "relay_tunnel_dialbacks_total",
		map[string]string{"result": metrics.DialbackHandshakeInvalid}); got != 1 {
		t.Errorf("dialbacks{handshake_invalid} = %v, want 1", got)
	}
}
