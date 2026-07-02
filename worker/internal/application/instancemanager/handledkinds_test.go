package instancemanager

import (
	"context"
	"strings"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// handledKinds is the canonical set of command kinds Manager.Handle dispatches.
// It is the single source the drift guard checks both layers against: the
// session-layer filter session.IsHandledKind must accept every one of these
// (otherwise the command is answered with the canned "unsupported" result and
// never reaches Handle — the bug in issue #219), and Manager.Handle must not
// fall through to its "unhandled command" default for any of them.
var handledKinds = []string{
	"StartServer", "StopServer", "RestartServer", "ServerCommand",
	"HydrateTrigger", "SnapshotTrigger", "ReadFile", "EditFile", "ListFiles",
	"TunnelDial",
}

// unhandledPrefix is the message Manager.Handle returns for a kind its switch
// does not recognize; the guard uses it to tell a real handler (which may still
// fail for other reasons) from the unhandled-command fallback.
const unhandledPrefix = "instancemanager: unhandled command"

// TestHandledKindsReachSessionFilter guards against the two layers drifting
// apart: the session dispatch filter (session.IsHandledKind) and the handler's
// own switch (Manager.Handle) must agree on which kinds are handled. A kind the
// handler accepts but the filter omits is silently answered "unsupported" and
// never dispatched (issue #219).
func TestHandledKindsReachSessionFilter(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)

	for _, kind := range handledKinds {
		// The session layer must dispatch this kind to the handler.
		if !session.IsHandledKind(kind) {
			t.Errorf("session.IsHandledKind(%q) = false; the handler accepts it but the session would answer it unsupported", kind)
		}

		// The handler must actually recognize this kind (not hit the default
		// "unhandled command" arm), confirming handledKinds tracks the switch.
		res := m.Handle(context.Background(), session.Command{CommandID: "c", ServerID: "s", Kind: kind})
		if !res.Success && strings.HasPrefix(res.ErrorMessage, unhandledPrefix) {
			t.Errorf("Manager.Handle(%q) hit the unhandled-command default; handledKinds is out of sync with the switch", kind)
		}
	}
}

// TestUnknownKindIsUnhandled is the negative control: a kind in neither list is
// rejected by both layers, so the guard above is meaningful (it is not asserting
// a tautology that holds for every string).
func TestUnknownKindIsUnhandled(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)

	const unknown = "NoSuchCommand"
	if session.IsHandledKind(unknown) {
		t.Errorf("session.IsHandledKind(%q) = true, want false", unknown)
	}
	res := m.Handle(context.Background(), session.Command{CommandID: "c", ServerID: "s", Kind: unknown})
	if res.Success || !strings.HasPrefix(res.ErrorMessage, unhandledPrefix) {
		t.Errorf("Manager.Handle(%q) = %+v, want unhandled-command failure", unknown, res)
	}
}

// TestBedrockTunnelKindsAreUnhandled guards the "no-op/unimplemented handler"
// requirement for OpenBedrockTunnel/CloseBedrockTunnel (issue #1544): the QUIC
// client that will actually act on these lands in issue #1546, so until then
// both kinds must fall through the same unhandled-command path as any other
// unrecognized kind (session.IsHandledKind false, Manager.Handle's default
// arm) rather than panicking or being silently dropped -- the control stream
// keeps working and the API sees a clear rejection.
func TestBedrockTunnelKindsAreUnhandled(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)

	for _, kind := range []string{"OpenBedrockTunnel", "CloseBedrockTunnel"} {
		if session.IsHandledKind(kind) {
			t.Errorf("session.IsHandledKind(%q) = true, want false (issue #1546 not landed yet)", kind)
		}
		res := m.Handle(context.Background(), session.Command{CommandID: "c", ServerID: "s", Kind: kind})
		if res.Success || !strings.HasPrefix(res.ErrorMessage, unhandledPrefix) {
			t.Errorf("Manager.Handle(%q) = %+v, want unhandled-command failure", kind, res)
		}
	}
}
