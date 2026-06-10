package instancemanager

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// TestHandleRejectsUnsafeServerID covers the command-intake guard (issue #782):
// an empty, traversal, or separator-bearing ServerID is rejected before any
// handler joins it into a scratch path, so SnapshotTrigger never tars the
// scratch ROOT and HydrateTrigger never escapes it. The reject must carry
// FILE_ACCESS_DENIED and leave the scratch tree untouched.
func TestHandleRejectsUnsafeServerID(t *testing.T) {
	cases := []struct {
		name     string
		serverID string
	}{
		{"empty", ""},
		{"dotdot", ".."},
		{"parent_escape", "../x"},
		{"slash", "a/b"},
		{"backslash", `a\b`},
		{"nul", "a\x00b"},
		{"dot", "."},
	}

	// Every handled kind funnels through Manager.Handle, so the guard must reject
	// the bad id regardless of kind (including the file ops).
	kinds := []string{
		"StartServer", "StopServer", "RestartServer", "ServerCommand",
		"HydrateTrigger", "SnapshotTrigger", "ReadFile", "EditFile", "ListFiles",
	}

	for _, tc := range cases {
		for _, kind := range kinds {
			t.Run(tc.name+"/"+kind, func(t *testing.T) {
				m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{})
				cmd := contractCmd(t, kind, tc.serverID)

				res := m.Handle(context.Background(), cmd)

				if res.Success {
					t.Fatalf("Handle(%s, %q) succeeded, want rejection", kind, tc.serverID)
				}
				if res.ErrorCode != session.CommandErrorFileAccessDenied {
					t.Fatalf("ErrorCode = %v, want CommandErrorFileAccessDenied", res.ErrorCode)
				}
				// No filesystem effect: the scratch root must contain nothing the
				// guard let a handler create.
				entries, err := os.ReadDir(m.scratchDir)
				if err != nil {
					t.Fatalf("read scratch dir: %v", err)
				}
				if len(entries) != 0 {
					t.Fatalf("scratch dir not empty after rejected command: %v", entries)
				}
			})
		}
	}
}

// TestHandleAcceptsUUIDServerID confirms a normal canonical UUID id (what the
// API sends: str(uuid)) passes the guard and reaches the handler, which creates
// the working dir under scratch.
func TestHandleAcceptsUUIDServerID(t *testing.T) {
	const uuidID = "123e4567-e89b-12d3-a456-426614174000"
	d := &fakeDriver{}
	m := newManager(t, d, nil)

	cmd := startCmd()
	cmd.ServerID = uuidID
	res := m.Handle(context.Background(), cmd)

	if !res.Success {
		t.Fatalf("Handle(StartServer, %q) = %+v, want success", uuidID, res)
	}
	if info, err := os.Stat(filepath.Join(m.scratchDir, uuidID)); err != nil || !info.IsDir() {
		t.Fatalf("working dir for %q not created: %v", uuidID, err)
	}
}
