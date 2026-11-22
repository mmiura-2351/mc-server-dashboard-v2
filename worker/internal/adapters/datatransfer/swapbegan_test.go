package datatransfer

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
)

// A failed hydrate says whether its swap began (issue #4041). An error from before
// the first rename that moves the working dir aside or the new tree in wraps
// scratchformat.ErrBeforeSwap: the working dir is as it was. An error from that rename
// on does not, as the working dir may then be changed or absent. The API puts back the
// awaiting_hydrate mark only for the first, so a stage that says "before" too late lets
// a snapshot publish a working dir the hydrate may have replaced, and one that stops
// saying it too early costs a stop its final snapshot for nothing.
//
// One case fails each stage. The last one before the swap is the displaced slot's
// check, and the first one of the swap is the rename that parks the live set aside.
func TestHydrateFailureSaysWhetherItsSwapBegan(t *testing.T) {
	failing := errors.New("injected")
	good := tarOf(map[string]string{"server.properties": "new"})
	cases := []struct {
		name string
		// status and body are the data plane's answer; closed closes the server
		// before the request.
		status int
		body   []byte
		closed bool
		// live puts a working set at the working dir, and slot a recovery tree at
		// its .displaced- slot, before the hydrate.
		live, slot bool
		// inject fails one step of the swap, given the working dir.
		inject     func(t *testing.T, dest string)
		beforeSwap bool
	}{
		{name: "request fails", closed: true, beforeSwap: true},
		{name: "pull refused", status: http.StatusInternalServerError, beforeSwap: true},
		{
			name: "archive refused", status: http.StatusOK,
			body: tarOf(map[string]string{"/abs": "x"}), live: true, beforeSwap: true,
		},
		{
			name: "displaced slot unreadable", status: http.StatusOK, body: good, live: true, slot: true,
			inject: func(t *testing.T, dest string) {
				displaced := displacedDir(dest)
				orig := readDir
				readDir = func(dir string) ([]os.DirEntry, error) {
					if dir == displaced {
						return nil, os.ErrPermission
					}
					return os.ReadDir(dir)
				}
				t.Cleanup(func() { readDir = orig })
			},
			beforeSwap: true,
		},
		{
			name: "aside rename fails", status: http.StatusOK, body: good, live: true,
			inject: func(t *testing.T, _ string) {
				orig := asideRename
				asideRename = func(_, _ string) error { return failing }
				t.Cleanup(func() { asideRename = orig })
			},
		},
		{
			name: "aside rename of a superseded set fails", status: http.StatusOK, body: good, live: true, slot: true,
			inject: func(t *testing.T, _ string) {
				orig := asideRename
				asideRename = func(_, _ string) error { return failing }
				t.Cleanup(func() { asideRename = orig })
			},
		},
		{
			name: "swap-in fails over a live set", status: http.StatusOK, body: good, live: true,
			inject: func(t *testing.T, _ string) {
				orig := swapRename
				swapRename = func(_, _ string) error { return failing }
				t.Cleanup(func() { swapRename = orig })
			},
		},
		{
			name: "swap-in fails onto an absent working dir", status: http.StatusOK, body: good,
			inject: func(t *testing.T, _ string) {
				orig := swapRename
				swapRename = func(_, _ string) error { return failing }
				t.Cleanup(func() { swapRename = orig })
			},
		},
		{
			name: "fsync after the swap fails", status: http.StatusOK, body: good, live: true,
			inject: func(t *testing.T, _ string) {
				orig := fsyncParent
				fsyncParent = func(string) error { return failing }
				t.Cleanup(func() { fsyncParent = orig })
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write(tc.body)
			}))
			if tc.closed {
				srv.Close()
			} else {
				defer srv.Close()
			}
			scratch := t.TempDir()
			dest := filepath.Join(scratch, "server")
			if tc.live {
				writeTree(t, dest, map[string]string{"server.properties": "live"})
			}
			if tc.slot {
				writeTree(t, displacedDir(dest), map[string]string{"server.properties": "retained"})
			}
			if tc.inject != nil {
				tc.inject(t, dest)
			}

			_, err := New(srv.Client()).Hydrate(context.Background(), srv.URL, "tok", dest, plainMarker)

			if err == nil {
				t.Fatal("Hydrate succeeded, want the injected failure")
			}
			if got := errors.Is(err, scratchformat.ErrBeforeSwap); got != tc.beforeSwap {
				t.Fatalf("errors.Is(%q, ErrBeforeSwap) = %v, want %v", err, got, tc.beforeSwap)
			}
			if tc.beforeSwap {
				assertWorkingDirAsItWas(t, dest, tc.live)
			}
		})
	}
}

// assertWorkingDirAsItWas checks what "before the swap" promises: the working dir
// still holds exactly the live set, or is still absent.
func assertWorkingDirAsItWas(t *testing.T, dest string, live bool) {
	t.Helper()
	if !live {
		if _, err := os.Lstat(dest); !os.IsNotExist(err) {
			t.Fatalf("working dir %s exists after a failure before the swap (Lstat: %v), want it absent", dest, err)
		}
		return
	}
	entries, err := os.ReadDir(dest)
	if err != nil || len(entries) != 1 || entries[0].Name() != "server.properties" {
		t.Fatalf("working dir entries = %v (%v), want only the live server.properties", entries, err)
	}
	if got, err := os.ReadFile(filepath.Join(dest, "server.properties")); err != nil || string(got) != "live" {
		t.Fatalf("working dir server.properties = %q (%v), want %q", got, err, "live")
	}
}
