package controlplane

import (
	"reflect"
	"testing"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// toCommand carries the oneof's name into Kind and every payload field the
// instancemanager handler reads onto the domain command; fields the command does
// not carry stay zero. Comparing the whole struct pins both halves.
func TestToCommandMapsPayload(t *testing.T) {
	tests := []struct {
		name string
		cmd  *controlplanev1.ApiCommand
		want session.Command
	}{
		{
			// An unset launch_mode (UNSPECIFIED) maps to the empty launch mode, which the instancemanager treats as the
			// historical JAR launch. Unset memory_limit_bytes / cpu_millis stay 0.
			name: "start with defaults",
			cmd: &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_Start{Start: &controlplanev1.StartServer{
				Driver:           controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_CONTAINER,
				JarRelpath:       "server.jar",
				MinecraftVersion: "1.21",
			}}},
			want: session.Command{
				Kind: "StartServer", Driver: "container", LaunchMode: "",
				JarRelpath: "server.jar", MinecraftVersion: "1.21",
			},
		},
		{
			name: "start with jar launch mode",
			cmd: &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_Start{Start: &controlplanev1.StartServer{
				LaunchMode: controlplanev1.LaunchMode_LAUNCH_MODE_JAR,
			}}},
			want: session.Command{Kind: "StartServer", LaunchMode: "jar"},
		},
		{
			name: "start with forge argsfile launch mode",
			cmd: &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_Start{Start: &controlplanev1.StartServer{
				LaunchMode: controlplanev1.LaunchMode_LAUNCH_MODE_FORGE_ARGSFILE,
			}}},
			want: session.Command{Kind: "StartServer", LaunchMode: "forge-argsfile"},
		},
		{
			// The per-server memory ceiling and soft CPU allocation are carried unchanged.
			name: "start with resource limits",
			cmd: &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_Start{Start: &controlplanev1.StartServer{
				MemoryLimitBytes: 2048 * 1024 * 1024,
				CpuMillis:        2000,
			}}},
			want: session.Command{Kind: "StartServer", MemoryLimitBytes: 2048 * 1024 * 1024, CPUMillis: 2000},
		},
		{
			name: "stop with force",
			cmd:  &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_Stop{Stop: &controlplanev1.StopServer{Force: true}}},
			want: session.Command{Kind: "StopServer", Force: true},
		},
		{
			name: "restart",
			cmd:  &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_Restart{Restart: &controlplanev1.RestartServer{}}},
			want: session.Command{Kind: "RestartServer"},
		},
		{
			name: "server command",
			cmd:  &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_ServerCommand{ServerCommand: &controlplanev1.ServerCommand{Line: "say hi"}}},
			want: session.Command{Kind: "ServerCommand", Line: "say hi"},
		},
		{
			name: "hydrate trigger",
			cmd: &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_Hydrate{Hydrate: &controlplanev1.HydrateTrigger{
				TransferUrl: "https://api/working-set", TransferToken: "tok",
			}}},
			want: session.Command{Kind: "HydrateTrigger", TransferURL: "https://api/working-set", TransferToken: "tok"},
		},
		{
			name: "snapshot trigger",
			cmd: &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_Snapshot{Snapshot: &controlplanev1.SnapshotTrigger{
				TransferUrl: "https://api/snapshot", TransferToken: "tok",
			}}},
			want: session.Command{Kind: "SnapshotTrigger", TransferURL: "https://api/snapshot", TransferToken: "tok"},
		},
		{
			name: "read file",
			cmd:  &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_ReadFile{ReadFile: &controlplanev1.ReadFile{Path: "server.properties"}}},
			want: session.Command{Kind: "ReadFile", Path: "server.properties"},
		},
		{
			name: "edit file",
			cmd:  &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_EditFile{EditFile: &controlplanev1.EditFile{Path: "ops.json", Content: []byte("[]")}}},
			want: session.Command{Kind: "EditFile", Path: "ops.json", Content: []byte("[]")},
		},
		{
			name: "list files",
			cmd:  &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_ListFiles{ListFiles: &controlplanev1.ListFiles{Path: "plugins"}}},
			want: session.Command{Kind: "ListFiles", Path: "plugins"},
		},
		{
			name: "tunnel dial",
			cmd: &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_TunnelDial{TunnelDial: &controlplanev1.TunnelDial{
				ServerId: "s1", Endpoint: "relay.example:25665", Token: "tok-abc", TlsCaPem: "ca-pem",
			}}},
			want: session.Command{
				Kind: "TunnelDial", TunnelEndpoint: "relay.example:25665", TunnelToken: "tok-abc", TunnelCAPEM: "ca-pem",
			},
		},
		{
			// OpenBedrockTunnel / CloseBedrockTunnel carry the credential the bedrocktunnel QUIC client dials the relay
			// with.
			name: "open bedrock tunnel",
			cmd: &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_OpenBedrockTunnel{OpenBedrockTunnel: &controlplanev1.OpenBedrockTunnel{
				ServerId: "s1", RelayEndpoint: "relay.example:25675", BedrockPort: 19132, Token: "tok-abc", TlsCaPem: "ca-pem",
			}}},
			want: session.Command{
				Kind: "OpenBedrockTunnel", BedrockRelayEndpoint: "relay.example:25675", BedrockPort: 19132,
				BedrockToken: "tok-abc", BedrockCAPEM: "ca-pem",
			},
		},
		{
			name: "close bedrock tunnel",
			cmd:  &controlplanev1.ApiCommand{Command: &controlplanev1.ApiCommand_CloseBedrockTunnel{CloseBedrockTunnel: &controlplanev1.CloseBedrockTunnel{ServerId: "s1"}}},
			want: session.Command{Kind: "CloseBedrockTunnel"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			tc.cmd.CommandId, tc.cmd.ServerId = "c1", "s1"
			got := toCommand(tc.cmd)
			want := tc.want
			want.CommandID, want.ServerID = "c1", "s1"
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("toCommand =\n  %+v\nwant\n  %+v", got, want)
			}
		})
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

// Every domain error code maps onto its own wire value, and an unrecognized code
// falls back to INTERNAL.
func TestMapErrorCode(t *testing.T) {
	tests := []struct {
		name string
		code session.CommandErrorCode
		want controlplanev1.CommandErrorCode
	}{
		{"internal", session.CommandErrorInternal, controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_INTERNAL},
		{"server not found", session.CommandErrorServerNotFound, controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_SERVER_NOT_FOUND},
		{"invalid state", session.CommandErrorInvalidState, controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_INVALID_STATE},
		{"driver unavailable", session.CommandErrorDriverUnavailable, controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_DRIVER_UNAVAILABLE},
		{"transfer failed", session.CommandErrorTransferFailed, controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_TRANSFER_FAILED},
		{"file access denied", session.CommandErrorFileAccessDenied, controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_FILE_ACCESS_DENIED},
		{"port conflict", session.CommandErrorPortConflict, controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_PORT_CONFLICT},
		{"image missing", session.CommandErrorImageMissing, controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_IMAGE_MISSING},
		{"busy", session.CommandErrorBusy, controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_BUSY},
		{"unrecognized", session.CommandErrorCode(99), controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_INTERNAL},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := mapErrorCode(tc.code); got != tc.want {
				t.Fatalf("mapErrorCode(%v) = %v, want %v", tc.code, got, tc.want)
			}
		})
	}
}

// mapFileAccessReason translates each domain reason onto the wire enum so the API can surface an honest problem
// reason instead of a blanket invalid_path.
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
