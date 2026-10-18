package instancemanager

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/rcon"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/serverproperties"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// These tests drive a server.properties the server's own code replaced (issue
// #3347) through the real reader and, for RCON, the real rcon adapter, so what the
// user is told is what a planted file really produces.

// plantedSecret is content of the file a planted server.properties points at or
// carries; no result may ever repeat it.
const plantedSecret = "the-other-servers-rcon-password"

// refusedServerProperties are the two shapes the issue names: a link to another
// server's file and a file over the reader's size cap.
var refusedServerProperties = map[string]func(t *testing.T, path string){
	"symlink to another server's file": func(t *testing.T, path string) {
		t.Helper()
		other := filepath.Join(t.TempDir(), "server.properties")
		if err := os.WriteFile(other, []byte("enable-rcon=true\nrcon.password="+plantedSecret+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			t.Fatal(err)
		}
		if err := os.Symlink(other, path); err != nil {
			t.Fatal(err)
		}
	},
	"over the size cap": func(t *testing.T, path string) {
		t.Helper()
		body := "enable-rcon=true\nrcon.password=" + plantedSecret + "\n#" + strings.Repeat("x", serverproperties.MaxSize)
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	},
}

// assertServerPropertiesUnreadable fails unless res is the dedicated refusal and
// carries none of the planted content.
func assertServerPropertiesUnreadable(t *testing.T, res session.CommandResult) {
	t.Helper()
	if res.Success {
		t.Fatalf("result = %+v, want a failure", res)
	}
	if res.ErrorCode != session.CommandErrorServerPropertiesUnreadable {
		t.Errorf("ErrorCode = %v, want server_properties_unreadable (message %q)", res.ErrorCode, res.ErrorMessage)
	}
	if strings.Contains(res.ErrorMessage, plantedSecret) {
		t.Errorf("ErrorMessage carries the file's content: %q", res.ErrorMessage)
	}
}

// openFromScratch is cmd/worker's RCON opener: the real rcon adapter over the
// server's working dir under m's scratch root.
func openFromScratch(m *Manager) controlFunc {
	return func(ctx context.Context, serverID, _, mcVersion string) (execution.ServerControl, error) {
		return rcon.OpenFromWorkingDir(ctx, filepath.Join(m.scratchDir, serverID), "", mcVersion)
	}
}

func TestStartNamesARefusedServerProperties(t *testing.T) {
	for name, plant := range refusedServerProperties {
		t.Run(name, func(t *testing.T) {
			// The driver fails its Start exactly as the container driver does: with the
			// reader's own error for the planted file, wrapped.
			dir := filepath.Join(t.TempDir(), s1)
			if err := os.MkdirAll(dir, 0o750); err != nil {
				t.Fatal(err)
			}
			plant(t, filepath.Join(dir, serverproperties.FileName))
			_, readErr := serverproperties.Read(dir)
			if readErr == nil {
				t.Fatal("the reader accepted the planted file")
			}
			m := newManager(t, &fakeDriver{startErr: fmt.Errorf("containerdriver: %w", readErr)}, nil)
			seedScratch(t, m, s1)

			assertServerPropertiesUnreadable(t, m.Handle(context.Background(), startCmd()))
		})
	}
}

func TestServerCommandNamesARefusedServerProperties(t *testing.T) {
	for name, plant := range refusedServerProperties {
		t.Run(name, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, nil)
			m.openControl = openFromScratch(m)
			startRunning(t, m)
			plant(t, filepath.Join(m.scratchDir, s1, serverproperties.FileName))

			res := m.Handle(context.Background(), session.Command{CommandID: "c2", ServerID: s1, Kind: "ServerCommand", Line: "list"})
			assertServerPropertiesUnreadable(t, res)
		})
	}
}

// A graceful stop cannot flush the world first (RCON's credentials are in the
// refused file), but it is not failed for that: the driver falls back to its stop
// signal. That signal may end the server before its own shutdown save completes
// (issue #3436), so the one WARN telling the operator about the skipped flush says
// changes can be lost and promises no save (issue #3347).
func TestStopWithARefusedServerPropertiesWarnsOnceAndStops(t *testing.T) {
	for name, plant := range refusedServerProperties {
		t.Run(name, func(t *testing.T) {
			d := &rconFailDriver{}
			h := &capturingSlogHandler{}
			m := newManager(t, d, nil).WithLogger(slog.New(h))
			m.openControl = openFromScratch(m)
			startRunning(t, m)
			plant(t, filepath.Join(m.scratchDir, s1, serverproperties.FileName))
			h.records = nil

			res := m.Handle(context.Background(), session.Command{CommandID: "c2", ServerID: s1, Kind: "StopServer"})
			if !res.Success {
				t.Fatalf("stop = %+v, want success: the flush is best-effort", res)
			}
			if stopped, graceful := d.inst.wasStopped(); !stopped || !graceful {
				t.Fatalf("instance stopped=%v graceful=%v, want a graceful stop", stopped, graceful)
			}

			var flushWarns []string
			for _, r := range h.records {
				if r.Level == slog.LevelWarn && strings.HasPrefix(r.Message, "stop flush:") {
					flushWarns = append(flushWarns, r.Message)
				}
			}
			if len(flushWarns) != 1 {
				t.Fatalf("stop flush WARNs = %q, want exactly one", flushWarns)
			}
			for _, want := range []string{"server.properties", "skipping the final save-all", "can be lost"} {
				if !strings.Contains(flushWarns[0], want) {
					t.Errorf("stop flush WARN = %q, want it to mention %q", flushWarns[0], want)
				}
			}
			if strings.Contains(flushWarns[0], "still triggers") {
				t.Errorf("stop flush WARN = %q promises a shutdown save the signal may not complete", flushWarns[0])
			}
		})
	}
}
