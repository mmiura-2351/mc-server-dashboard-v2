package controlplane

import (
	"testing"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
)

func TestToCommandMapsHydrateTrigger(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c1",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_Hydrate{
			Hydrate: &controlplanev1.HydrateTrigger{
				TransferUrl:   "https://api/working-set",
				TransferToken: "tok",
			},
		},
	})
	if cmd.Kind != "HydrateTrigger" {
		t.Fatalf("Kind = %q, want HydrateTrigger", cmd.Kind)
	}
	if cmd.TransferURL != "https://api/working-set" || cmd.TransferToken != "tok" {
		t.Fatalf("transfer fields = %q/%q", cmd.TransferURL, cmd.TransferToken)
	}
}

func TestToCommandMapsSnapshotTrigger(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c2",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_Snapshot{
			Snapshot: &controlplanev1.SnapshotTrigger{
				TransferUrl:   "https://api/snapshot",
				TransferToken: "tok",
			},
		},
	})
	if cmd.Kind != "SnapshotTrigger" {
		t.Fatalf("Kind = %q, want SnapshotTrigger", cmd.Kind)
	}
	if cmd.TransferURL != "https://api/snapshot" || cmd.TransferToken != "tok" {
		t.Fatalf("transfer fields = %q/%q", cmd.TransferURL, cmd.TransferToken)
	}
}
