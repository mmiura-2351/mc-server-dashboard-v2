package instancemanager

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// fakeTunnelDialer records the TunnelSpec it was asked to dial and returns a
// configurable error, standing in for the relay dial-back adapter.
type fakeTunnelDialer struct {
	mu   sync.Mutex
	spec TunnelSpec
	err  error
	dial int
}

func (f *fakeTunnelDialer) Dial(_ context.Context, spec TunnelSpec) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.dial++
	f.spec = spec
	return f.err
}

func (f *fakeTunnelDialer) lastSpec() (TunnelSpec, int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.spec, f.dial
}

func tunnelCmd() session.Command {
	return session.Command{
		CommandID:      "t1",
		ServerID:       "s1",
		Kind:           "TunnelDial",
		TunnelEndpoint: "relay.example:25665",
		TunnelToken:    "tok-abc",
		TunnelCAPEM:    "ca-pem",
	}
}

// startRunning launches s1 so it is a running instance the tunnel handler resolves.
func startRunning(t *testing.T, m *Manager) {
	t.Helper()
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}
}

// A TunnelDial against a running server delegates to the dialer with the resolved
// working dir and the command's relay endpoint/token/CA, and succeeds (RELAY.md
// Section 5).
func TestTunnelDialDelegatesToDialer(t *testing.T) {
	d := &fakeDriver{}
	dialer := &fakeTunnelDialer{}
	m := newManager(t, d, nil).WithTunnelDialer(dialer)
	startRunning(t, m)

	res := m.Handle(context.Background(), tunnelCmd())
	if !res.Success {
		t.Fatalf("TunnelDial = %+v, want success", res)
	}
	spec, calls := dialer.lastSpec()
	if calls != 1 {
		t.Fatalf("dialer called %d times, want 1", calls)
	}
	if spec.ServerID != "s1" || spec.Endpoint != "relay.example:25665" ||
		spec.Token != "tok-abc" || spec.CAPEM != "ca-pem" {
		t.Fatalf("dialer spec = %+v, missing command fields", spec)
	}
	if spec.WorkingDir == "" {
		t.Fatalf("dialer spec working dir empty, want the server's scratch dir")
	}
}

// A TunnelDial for a server that is not running locally fails SERVER_NOT_FOUND and
// never reaches the dialer (RELAY.md Section 5).
func TestTunnelDialServerNotRunning(t *testing.T) {
	dialer := &fakeTunnelDialer{}
	m := newManager(t, &fakeDriver{}, nil).WithTunnelDialer(dialer)

	res := m.Handle(context.Background(), tunnelCmd())
	if res.Success {
		t.Fatalf("TunnelDial on a not-running server = success, want failure")
	}
	if res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("ErrorCode = %v, want server_not_found", res.ErrorCode)
	}
	if _, calls := dialer.lastSpec(); calls != 0 {
		t.Fatalf("dialer called %d times for a not-running server, want 0", calls)
	}
}

// A dial/handshake failure surfaces as an INTERNAL CommandResult error.
func TestTunnelDialFailureSurfacesError(t *testing.T) {
	dialer := &fakeTunnelDialer{err: errors.New("relay refused handshake")}
	m := newManager(t, &fakeDriver{}, nil).WithTunnelDialer(dialer)
	startRunning(t, m)

	res := m.Handle(context.Background(), tunnelCmd())
	if res.Success {
		t.Fatalf("TunnelDial with a failing dialer = success, want failure")
	}
	if res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("ErrorCode = %v, want internal", res.ErrorCode)
	}
}

// Without a wired dialer a TunnelDial fails internally rather than panicking.
func TestTunnelDialUnconfigured(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	startRunning(t, m)

	res := m.Handle(context.Background(), tunnelCmd())
	if res.Success || res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("TunnelDial without a dialer = %+v, want internal failure", res)
	}
}
