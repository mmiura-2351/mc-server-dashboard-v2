package controlplane

import (
	"testing"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
)

// Every domain status name the session can emit maps onto its own wire value, and
// an unrecognized name still falls through to UNSPECIFIED. Pinning the whole table
// is what keeps a later edit from silently rerouting one state onto another's wire
// value -- a misclassification the API would cache as fact (issue #2474).
func TestMapServerState(t *testing.T) {
	cases := map[string]controlplanev1.ServerState{
		"starting":   controlplanev1.ServerState_SERVER_STATE_STARTING,
		"running":    controlplanev1.ServerState_SERVER_STATE_RUNNING,
		"stopping":   controlplanev1.ServerState_SERVER_STATE_STOPPING,
		"stopped":    controlplanev1.ServerState_SERVER_STATE_STOPPED,
		"restarting": controlplanev1.ServerState_SERVER_STATE_RESTARTING,
		"crashed":    controlplanev1.ServerState_SERVER_STATE_CRASHED,
		// The Worker asserts unknown when it cannot confirm an instance's fate
		// (issue #2474). Falling through to UNSPECIFIED here would have the API
		// ingest drop the report entirely.
		"unknown":     controlplanev1.ServerState_SERVER_STATE_UNKNOWN,
		"not-a-state": controlplanev1.ServerState_SERVER_STATE_UNSPECIFIED,
	}
	for name, want := range cases {
		if got := mapServerState(name); got != want {
			t.Errorf("mapServerState(%q) = %v, want %v", name, got, want)
		}
	}
}
