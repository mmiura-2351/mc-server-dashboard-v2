package tunnel

import (
	"context"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// A server.properties the server planted — a link (to a file outside the working
// set, or to nothing), or a FIFO — fails the dial naming the file, without
// reading the link's target, blocking the lane, or falling back to the 25565
// default (issue #3335).
func TestDialRefusesAPlantedServerProperties(t *testing.T) {
	outside := filepath.Join(t.TempDir(), "other-server.properties")
	if err := os.WriteFile(outside, []byte("server-port=26999\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, plant := range map[string]func(path string) error{
		"symlink to an outside file": func(path string) error { return os.Symlink(outside, path) },
		"dangling symlink":           func(path string) error { return os.Symlink(outside+".absent", path) },
		"fifo":                       func(path string) error { return unix.Mkfifo(path, 0o600) },
	} {
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()

			relay := newFakeRelay(t, "tok")
			workingDir := t.TempDir()
			if err := plant(filepath.Join(workingDir, "server.properties")); err != nil {
				t.Fatal(err)
			}
			d := New(ctx, "0.0.0.0", nil, discardLogger(t))

			var dialed atomic.Bool
			d.gameDial = func(context.Context, string) (net.Conn, error) {
				dialed.Store(true)
				return nil, io.ErrUnexpectedEOF
			}

			done := make(chan error, 1)
			go func() {
				done <- d.Dial(ctx, Spec{
					ServerID: "s1", WorkingDir: workingDir,
					Endpoint: relay.addr(), Token: "tok", CAPEM: relay.caPEM,
				})
			}()
			var err error
			select {
			case err = <-done:
			case <-time.After(5 * time.Second):
				t.Fatal("Dial blocked on the planted server.properties")
			}
			if err == nil {
				t.Fatal("Dial = nil error, want the planted server.properties refused")
			}
			if !strings.Contains(err.Error(), "server.properties") {
				t.Errorf("Dial error = %v, want it to name server.properties", err)
			}
			if dialed.Load() {
				t.Error("the game port was dialed; want no dial at all when the file is refused")
			}
		})
	}
}
