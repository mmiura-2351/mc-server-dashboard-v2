package instancemanager

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// fakeBedrockTunneler records every Open/Close call, standing in for the
// bedrocktunnel adapter.
type fakeBedrockTunneler struct {
	mu      sync.Mutex
	opened  []BedrockTunnelSpec
	openErr error
	closed  []string
}

func (f *fakeBedrockTunneler) Open(spec BedrockTunnelSpec) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.opened = append(f.opened, spec)
	return f.openErr
}

func (f *fakeBedrockTunneler) Close(serverID string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.closed = append(f.closed, serverID)
}

func (f *fakeBedrockTunneler) lastOpen() (BedrockTunnelSpec, int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.opened) == 0 {
		return BedrockTunnelSpec{}, 0
	}
	return f.opened[len(f.opened)-1], len(f.opened)
}

func (f *fakeBedrockTunneler) closeCalls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]string, len(f.closed))
	copy(out, f.closed)
	return out
}

func openBedrockCmd() session.Command {
	return session.Command{
		CommandID:            "b1",
		ServerID:             "s1",
		Kind:                 "OpenBedrockTunnel",
		BedrockRelayEndpoint: "relay.example:25675",
		BedrockPort:          19132,
		BedrockToken:         "tok-abc",
		BedrockCAPEM:         "ca-pem",
	}
}

func closeBedrockCmd() session.Command {
	return session.Command{CommandID: "b2", ServerID: "s1", Kind: "CloseBedrockTunnel"}
}

// An OpenBedrockTunnel against a running server delegates to the tunneler with
// the command's relay endpoint/port/token/CA and succeeds
// (docs/app/BEDROCK_TUNNEL.md Section 3).
func TestOpenBedrockTunnelDelegatesToTunneler(t *testing.T) {
	d := &fakeDriver{}
	bt := &fakeBedrockTunneler{}
	m := newManager(t, d, nil).WithBedrockTunneler(bt)
	startRunning(t, m)

	res := m.Handle(context.Background(), openBedrockCmd())
	if !res.Success {
		t.Fatalf("OpenBedrockTunnel = %+v, want success", res)
	}
	spec, calls := bt.lastOpen()
	if calls != 1 {
		t.Fatalf("Open called %d times, want 1", calls)
	}
	if spec.ServerID != "s1" || spec.RelayEndpoint != "relay.example:25675" ||
		spec.BedrockPort != 19132 || spec.Token != "tok-abc" || spec.CAPEM != "ca-pem" {
		t.Fatalf("Open spec = %+v, missing command fields", spec)
	}
}

// An OpenBedrockTunnel for a server that is not running locally fails
// SERVER_NOT_FOUND and never reaches the tunneler.
func TestOpenBedrockTunnelServerNotRunning(t *testing.T) {
	bt := &fakeBedrockTunneler{}
	m := newManager(t, &fakeDriver{}, nil).WithBedrockTunneler(bt)

	res := m.Handle(context.Background(), openBedrockCmd())
	if res.Success {
		t.Fatalf("OpenBedrockTunnel on a not-running server = success, want failure")
	}
	if res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("ErrorCode = %v, want server_not_found", res.ErrorCode)
	}
	if _, calls := bt.lastOpen(); calls != 0 {
		t.Fatalf("Open called %d times for a not-running server, want 0", calls)
	}
}

// A synchronous Open failure (e.g. a malformed tls_ca_pem) surfaces as an
// INTERNAL CommandResult error.
func TestOpenBedrockTunnelFailureSurfacesError(t *testing.T) {
	bt := &fakeBedrockTunneler{openErr: errors.New("bad CA")}
	m := newManager(t, &fakeDriver{}, nil).WithBedrockTunneler(bt)
	startRunning(t, m)

	res := m.Handle(context.Background(), openBedrockCmd())
	if res.Success {
		t.Fatalf("OpenBedrockTunnel with a failing tunneler = success, want failure")
	}
	if res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("ErrorCode = %v, want internal", res.ErrorCode)
	}
}

// Without a wired tunneler an OpenBedrockTunnel fails internally rather than
// panicking.
func TestOpenBedrockTunnelUnconfigured(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	startRunning(t, m)

	res := m.Handle(context.Background(), openBedrockCmd())
	if res.Success || res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("OpenBedrockTunnel without a tunneler = %+v, want internal failure", res)
	}
}

// A repeated OpenBedrockTunnel is dispatched to the tunneler every time
// (idempotency is Manager.Open's own responsibility, docs/app/BEDROCK_TUNNEL.md
// Section 3) -- the instancemanager handler itself is a thin, unconditional
// delegate.
func TestOpenBedrockTunnelRepeatedDelegatesEachTime(t *testing.T) {
	bt := &fakeBedrockTunneler{}
	m := newManager(t, &fakeDriver{}, nil).WithBedrockTunneler(bt)
	startRunning(t, m)

	for i := 0; i < 2; i++ {
		if res := m.Handle(context.Background(), openBedrockCmd()); !res.Success {
			t.Fatalf("OpenBedrockTunnel[%d] = %+v, want success", i, res)
		}
	}
	if _, calls := bt.lastOpen(); calls != 2 {
		t.Fatalf("Open called %d times, want 2", calls)
	}
}

// CloseBedrockTunnel delegates to the tunneler's Close for the command's
// server id and always succeeds.
func TestCloseBedrockTunnelDelegatesToTunneler(t *testing.T) {
	bt := &fakeBedrockTunneler{}
	m := newManager(t, &fakeDriver{}, nil).WithBedrockTunneler(bt)

	res := m.Handle(context.Background(), closeBedrockCmd())
	if !res.Success {
		t.Fatalf("CloseBedrockTunnel = %+v, want success", res)
	}
	if calls := bt.closeCalls(); len(calls) != 1 || calls[0] != "s1" {
		t.Fatalf("Close calls = %v, want [s1]", calls)
	}
}

// CloseBedrockTunnel succeeds even for a server this Worker has no tracked
// instance for (it arrives after the instance is already evicted, or for a
// server this Worker never opened a tunnel for) -- Close is idempotent
// (docs/app/BEDROCK_TUNNEL.md Section 3).
func TestCloseBedrockTunnelSucceedsWithoutRunningInstance(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)

	res := m.Handle(context.Background(), closeBedrockCmd())
	if !res.Success {
		t.Fatalf("CloseBedrockTunnel = %+v, want success even unconfigured/not running", res)
	}
}

// A successful StopServer also tears down the server's Bedrock tunnel locally
// (attemptStop), without waiting on a separate CloseBedrockTunnel dispatch to
// arrive (issue #1546, docs/app/BEDROCK_TUNNEL.md Section 3).
func TestStopServerClosesBedrockTunnel(t *testing.T) {
	d := &fakeDriver{}
	bt := &fakeBedrockTunneler{}
	m := newManager(t, d, nil).WithBedrockTunneler(bt)
	startRunning(t, m)

	res := m.Handle(context.Background(), session.Command{CommandID: "s", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("StopServer = %+v, want success", res)
	}
	if calls := bt.closeCalls(); len(calls) != 1 || calls[0] != "s1" {
		t.Fatalf("Close calls = %v, want [s1]", calls)
	}
}

// StopServer without a wired tunneler must not panic (the bedrock field is
// optional, same posture as tunnel).
func TestStopServerWithoutBedrockTunnelerConfigured(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	startRunning(t, m)

	res := m.Handle(context.Background(), session.Command{CommandID: "s", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("StopServer = %+v, want success", res)
	}
}
