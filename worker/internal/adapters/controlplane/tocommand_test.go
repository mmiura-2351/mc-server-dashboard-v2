package controlplane

import (
	"testing"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
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

func TestToCommandMapsReadFile(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c3",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_ReadFile{
			ReadFile: &controlplanev1.ReadFile{Path: "server.properties"},
		},
	})
	if cmd.Kind != "ReadFile" {
		t.Fatalf("Kind = %q, want ReadFile", cmd.Kind)
	}
	if cmd.Path != "server.properties" {
		t.Fatalf("Path = %q, want server.properties", cmd.Path)
	}
}

func TestToCommandMapsEditFile(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c4",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_EditFile{
			EditFile: &controlplanev1.EditFile{Path: "ops.json", Content: []byte("[]")},
		},
	})
	if cmd.Kind != "EditFile" {
		t.Fatalf("Kind = %q, want EditFile", cmd.Kind)
	}
	if cmd.Path != "ops.json" || string(cmd.Content) != "[]" {
		t.Fatalf("Path/Content = %q/%q", cmd.Path, cmd.Content)
	}
}

func TestToCommandMapsListFiles(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c5",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_ListFiles{
			ListFiles: &controlplanev1.ListFiles{Path: "plugins"},
		},
	})
	if cmd.Kind != "ListFiles" {
		t.Fatalf("Kind = %q, want ListFiles", cmd.Kind)
	}
	if cmd.Path != "plugins" {
		t.Fatalf("Path = %q, want plugins", cmd.Path)
	}
}

func TestToFileListingMapsEntries(t *testing.T) {
	wire := toFileListing(&session.FileListing{
		Entries: []session.FileEntry{
			{Name: "config.yml", IsDir: false, Size: 12},
			{Name: "data", IsDir: true, Size: 0},
		},
		Truncated: true,
	})
	if !wire.GetTruncated() {
		t.Fatal("Truncated not carried onto the wire listing")
	}
	if len(wire.GetEntries()) != 2 {
		t.Fatalf("entries = %d, want 2", len(wire.GetEntries()))
	}
	first := wire.GetEntries()[0]
	if first.GetName() != "config.yml" || first.GetIsDir() || first.GetSize() != 12 {
		t.Fatalf("first entry = %+v, want config.yml file size 12", first)
	}
}

func TestMapErrorCodeFileAccessDenied(t *testing.T) {
	got := mapErrorCode(session.CommandErrorFileAccessDenied)
	if got != controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_FILE_ACCESS_DENIED {
		t.Fatalf("mapErrorCode = %v, want FILE_ACCESS_DENIED", got)
	}
}

func TestMapErrorCodePortConflict(t *testing.T) {
	got := mapErrorCode(session.CommandErrorPortConflict)
	if got != controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_PORT_CONFLICT {
		t.Fatalf("mapErrorCode = %v, want PORT_CONFLICT", got)
	}
}

func TestMapErrorCodeImageMissing(t *testing.T) {
	got := mapErrorCode(session.CommandErrorImageMissing)
	if got != controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_IMAGE_MISSING {
		t.Fatalf("mapErrorCode = %v, want IMAGE_MISSING", got)
	}
}

func TestMapLogStream(t *testing.T) {
	if got := mapLogStream(session.LogStreamStdout); got != controlplanev1.LogStream_LOG_STREAM_STDOUT {
		t.Fatalf("stdout mapped to %v", got)
	}
	if got := mapLogStream(session.LogStreamStderr); got != controlplanev1.LogStream_LOG_STREAM_STDERR {
		t.Fatalf("stderr mapped to %v", got)
	}
}
