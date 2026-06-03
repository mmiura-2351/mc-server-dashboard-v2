// Package controlplane_test proves the generated control-plane stubs are
// consumable from the worker module: it imports the generated package and
// exercises a value from it. If `make proto-gen` is broken or stale, this test
// stops compiling.
package controlplane_test

import (
	"testing"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
)

func TestGeneratedStubsAreImportable(t *testing.T) {
	// A round-trippable message field proves the generated types link.
	msg := &controlplanev1.WorkerMessage{CorrelationId: "abc"}
	if msg.GetCorrelationId() != "abc" {
		t.Fatalf("GetCorrelationId() = %q, want %q", msg.GetCorrelationId(), "abc")
	}

	// The service descriptor proves the gRPC stubs link.
	if controlplanev1.WorkerService_ServiceDesc.ServiceName != "mcsd.controlplane.v1.WorkerService" {
		t.Fatalf("unexpected service name %q", controlplanev1.WorkerService_ServiceDesc.ServiceName)
	}
}
