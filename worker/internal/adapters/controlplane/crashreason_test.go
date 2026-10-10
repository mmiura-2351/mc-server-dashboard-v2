package controlplane

import (
	"context"
	"testing"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// Every crash reason the execution domain can emit maps onto its own wire value, and an empty or unrecognized
// name is UNSPECIFIED, an unclassified crash. The names come from execution.CrashReason.String, so the table is
// keyed by the domain values and cannot drift from them.
func TestMapCrashReason(t *testing.T) {
	cases := map[string]controlplanev1.CrashReason{
		execution.CrashReasonUnspecified.String():                  controlplanev1.CrashReason_CRASH_REASON_UNSPECIFIED,
		execution.CrashReasonForgeInstallFailed.String():           controlplanev1.CrashReason_CRASH_REASON_FORGE_INSTALL_FAILED,
		execution.CrashReasonForgeInstallOutOfMemory.String():      controlplanev1.CrashReason_CRASH_REASON_FORGE_INSTALL_OUT_OF_MEMORY,
		execution.CrashReasonForgeInstallJavaIncompatible.String(): controlplanev1.CrashReason_CRASH_REASON_FORGE_INSTALL_JAVA_INCOMPATIBLE,
		"not-a-reason": controlplanev1.CrashReason_CRASH_REASON_UNSPECIFIED,
	}
	if len(cases) != 5 {
		t.Fatalf("crash reason wire names collide: %d distinct names, want 5", len(cases))
	}
	for name, want := range cases {
		if got := mapCrashReason(name); got != want {
			t.Errorf("mapCrashReason(%q) = %v, want %v", name, got, want)
		}
	}
}

// A status change carries its crash reason onto the wire beside the detail.
func TestStatusChangeCarriesTheCrashReason(t *testing.T) {
	tr, stream := newCapturingTransport()

	if err := tr.SendStatusChange(context.Background(), session.StatusEvent{
		ServerID: "s1", State: "crashed", Detail: "boom",
		CrashReason: execution.CrashReasonForgeInstallOutOfMemory.String(),
	}); err != nil {
		t.Fatal(err)
	}

	if len(stream.sent) != 1 {
		t.Fatalf("sent %d messages, want 1", len(stream.sent))
	}
	sc := stream.sent[0].GetEvent().GetStatusChange()
	if got := sc.GetCrashReason(); got != controlplanev1.CrashReason_CRASH_REASON_FORGE_INSTALL_OUT_OF_MEMORY {
		t.Fatalf("crash_reason = %v, want FORGE_INSTALL_OUT_OF_MEMORY", got)
	}
	if sc.GetDetail() != "boom" {
		t.Fatalf("detail = %q, want it unchanged", sc.GetDetail())
	}
}
