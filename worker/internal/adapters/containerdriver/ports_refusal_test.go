package containerdriver

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/serverproperties"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// A server.properties the server planted — a link (to a file outside the working
// set, or to nothing), or a FIFO — fails the start naming the file, without
// reading the link's target, blocking, or falling back to the 25565 default
// (issue #3335).
func TestStartRefusesAPlantedServerProperties(t *testing.T) {
	outside := filepath.Join(t.TempDir(), "other-server.properties")
	if err := os.WriteFile(outside, []byte("server-port=26999\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, plant := range map[string]func(path string) error{
		"symlink to an outside file": func(path string) error { return os.Symlink(outside, path) },
		"dangling symlink":           func(path string) error { return os.Symlink(outside+".absent", path) },
		"fifo":                       func(path string) error { return unix.Mkfifo(path, 0o600) },
		"over the size cap": func(path string) error {
			return os.WriteFile(path, []byte("server-port=26999\n#"+strings.Repeat("x", serverproperties.MaxSize)), 0o600)
		},
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			if err := plant(filepath.Join(dir, "server.properties")); err != nil {
				t.Fatal(err)
			}
			docker := newFakeDocker()
			d := newTestDriver(docker, nil, errors.New("no rcon"))
			s := spec()
			s.WorkingDir = dir

			done := make(chan error, 1)
			go func() {
				_, err := d.Start(context.Background(), s)
				done <- err
			}()
			var err error
			select {
			case err = <-done:
			case <-time.After(5 * time.Second):
				t.Fatal("Start blocked on the planted server.properties")
			}
			if err == nil {
				t.Fatalf("Start = nil error (published %v), want the planted server.properties refused", docker.createSpec.Ports)
			}
			if !strings.Contains(err.Error(), "server.properties") {
				t.Errorf("Start error = %v, want it to name server.properties", err)
			}
			// The instance manager answers the start with its own code on this
			// sentinel (issue #3347).
			if !errors.Is(err, execution.ErrServerPropertiesUnreadable) {
				t.Errorf("Start error = %v, want it to be execution.ErrServerPropertiesUnreadable", err)
			}
			if docker.createCalls != 0 {
				t.Errorf("createCalls = %d, want 0 (no container created on a refused file)", docker.createCalls)
			}
		})
	}
}
