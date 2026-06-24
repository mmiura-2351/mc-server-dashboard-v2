package controlplane

import (
	"testing"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// An unset launch_mode (UNSPECIFIED) maps to the empty launch mode, which the
// instancemanager treats as the historical JAR launch (issue #305).
func TestToCommandStartDefaultLaunchModeIsEmpty(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c1",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_Start{
			Start: &controlplanev1.StartServer{
				Driver:           controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_CONTAINER,
				JarRelpath:       "server.jar",
				MinecraftVersion: "1.21",
			},
		},
	})
	if cmd.Kind != "StartServer" {
		t.Fatalf("Kind = %q, want StartServer", cmd.Kind)
	}
	if cmd.LaunchMode != "" {
		t.Fatalf("LaunchMode = %q, want empty (default JAR)", cmd.LaunchMode)
	}
}

// An explicit LAUNCH_MODE_JAR maps to the "jar" name.
func TestToCommandStartJarLaunchMode(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c1",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_Start{
			Start: &controlplanev1.StartServer{LaunchMode: controlplanev1.LaunchMode_LAUNCH_MODE_JAR},
		},
	})
	if cmd.LaunchMode != "jar" {
		t.Fatalf("LaunchMode = %q, want jar", cmd.LaunchMode)
	}
}

// LAUNCH_MODE_FORGE_ARGSFILE maps to the "forge-argsfile" name.
func TestToCommandStartForgeLaunchMode(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c1",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_Start{
			Start: &controlplanev1.StartServer{LaunchMode: controlplanev1.LaunchMode_LAUNCH_MODE_FORGE_ARGSFILE},
		},
	})
	if cmd.LaunchMode != "forge-argsfile" {
		t.Fatalf("LaunchMode = %q, want forge-argsfile", cmd.LaunchMode)
	}
}

// The wire memory_limit_bytes (the per-server ceiling, #706) is carried onto the
// domain command unchanged; unset (0) stays 0.
func TestToCommandStartCarriesMemoryLimitBytes(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c1",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_Start{
			Start: &controlplanev1.StartServer{MemoryLimitBytes: 2048 * 1024 * 1024},
		},
	})
	if cmd.MemoryLimitBytes != 2048*1024*1024 {
		t.Fatalf("MemoryLimitBytes = %d, want %d", cmd.MemoryLimitBytes, 2048*1024*1024)
	}
}

func TestToCommandStartMemoryLimitBytesDefaultsToZero(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c1",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_Start{
			Start: &controlplanev1.StartServer{},
		},
	})
	if cmd.MemoryLimitBytes != 0 {
		t.Fatalf("MemoryLimitBytes = %d, want 0 (unset)", cmd.MemoryLimitBytes)
	}
	if cmd.CPUMillis != 0 {
		t.Fatalf("CPUMillis = %d, want 0 (unset)", cmd.CPUMillis)
	}
}

// The wire cpu_millis (the per-server soft CPU allocation, #723) is carried onto
// the domain command unchanged; unset (0) stays 0.
func TestToCommandStartCarriesCPUMillis(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c1",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_Start{
			Start: &controlplanev1.StartServer{CpuMillis: 2000},
		},
	})
	if cmd.CPUMillis != 2000 {
		t.Fatalf("CPUMillis = %d, want 2000", cmd.CPUMillis)
	}
}

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

func TestToCommandMapsTunnelDial(t *testing.T) {
	cmd := toCommand(&controlplanev1.ApiCommand{
		CommandId: "c6",
		ServerId:  "s1",
		Command: &controlplanev1.ApiCommand_TunnelDial{
			TunnelDial: &controlplanev1.TunnelDial{
				ServerId: "s1",
				Endpoint: "relay.example:25665",
				Token:    "tok-abc",
				TlsCaPem: "ca-pem",
			},
		},
	})
	if cmd.Kind != "TunnelDial" {
		t.Fatalf("Kind = %q, want TunnelDial", cmd.Kind)
	}
	if cmd.TunnelEndpoint != "relay.example:25665" {
		t.Fatalf("TunnelEndpoint = %q, want relay.example:25665", cmd.TunnelEndpoint)
	}
	if cmd.TunnelToken != "tok-abc" {
		t.Fatalf("TunnelToken = %q, want tok-abc", cmd.TunnelToken)
	}
	if cmd.TunnelCAPEM != "ca-pem" {
		t.Fatalf("TunnelCAPEM = %q, want ca-pem", cmd.TunnelCAPEM)
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

func TestMapErrorCodeBusy(t *testing.T) {
	got := mapErrorCode(session.CommandErrorBusy)
	if got != controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_BUSY {
		t.Fatalf("mapErrorCode = %v, want BUSY", got)
	}
}

// mapFileAccessReason translates each domain reason onto the wire enum so the
// API can surface an honest problem reason instead of a blanket invalid_path
// (issue #548).
func TestMapFileAccessReason(t *testing.T) {
	cases := []struct {
		reason session.FileAccessReason
		want   controlplanev1.FileAccessReason
	}{
		{session.FileAccessReasonUnspecified, controlplanev1.FileAccessReason_FILE_ACCESS_REASON_UNSPECIFIED},
		{session.FileAccessReasonIsADirectory, controlplanev1.FileAccessReason_FILE_ACCESS_REASON_IS_A_DIRECTORY},
		{session.FileAccessReasonNotADirectory, controlplanev1.FileAccessReason_FILE_ACCESS_REASON_NOT_A_DIRECTORY},
		{session.FileAccessReasonSymlinkRefused, controlplanev1.FileAccessReason_FILE_ACCESS_REASON_SYMLINK_REFUSED},
		{session.FileAccessReasonPayloadTooLarge, controlplanev1.FileAccessReason_FILE_ACCESS_REASON_PAYLOAD_TOO_LARGE},
	}
	for _, c := range cases {
		if got := mapFileAccessReason(c.reason); got != c.want {
			t.Fatalf("mapFileAccessReason(%v) = %v, want %v", c.reason, got, c.want)
		}
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
