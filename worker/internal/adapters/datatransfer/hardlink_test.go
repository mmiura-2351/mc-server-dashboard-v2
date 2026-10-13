package datatransfer

import (
	"archive/tar"
	"bytes"
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

// A hydrate is the one way stored content reaches a working set, so these pin
// that it cannot bring a hard link with it: the launch hand-over re-owns inodes,
// and a working-set file sharing one with a file elsewhere would have that file
// re-owned too (issue #3338).

// A hard-link member is refused outright, whether it names a member of the same
// archive or a path outside it, and nothing is published.
func TestHydrateRejectsHardLinkMember(t *testing.T) {
	for _, linkname := range []string{"server.properties", "../outside", "/etc/passwd"} {
		t.Run(linkname, func(t *testing.T) {
			var buf bytes.Buffer
			tw := tar.NewWriter(&buf)
			_ = tw.WriteHeader(&tar.Header{Name: "server.properties", Typeflag: tar.TypeReg, Mode: 0o640, Size: 1})
			_, _ = tw.Write([]byte("x"))
			_ = tw.WriteHeader(&tar.Header{Name: "link", Typeflag: tar.TypeLink, Linkname: linkname})
			_ = tw.Close()
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				_, _ = w.Write(buf.Bytes())
			}))
			defer srv.Close()

			dest := filepath.Join(t.TempDir(), "working")
			c := New(srv.Client())
			if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err == nil {
				t.Fatal("expected an error for the hard-link member")
			}
			if _, err := os.Lstat(dest); !os.IsNotExist(err) {
				t.Fatalf("a working set was published from an archive with a hard-link member (lstat: %v)", err)
			}
		})
	}
}

// A snapshot of a working set that holds hard links — between two of its own
// files, and to a file outside it — restores as independent files: each name
// keeps the content and none shares an inode, with each other or with the
// outside file.
func TestSnapshotRoundTripCarriesNoHardLink(t *testing.T) {
	base := t.TempDir()
	src, outside := filepath.Join(base, "src"), filepath.Join(base, "outside")
	if err := os.MkdirAll(filepath.Join(src, "backups"), 0o750); err != nil {
		t.Fatal(err)
	}
	for path, content := range map[string]string{filepath.Join(src, "own"): "own", outside: "outside"} {
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	for name, target := range map[string]string{"backups/own": filepath.Join(src, "own"), "planted": outside} {
		if err := os.Link(target, filepath.Join(src, filepath.FromSlash(name))); err != nil {
			t.Fatal(err)
		}
	}

	var archive bytes.Buffer
	if err := packTar(src, &archive, slog.Default()); err != nil {
		t.Fatalf("pack: %v", err)
	}
	dest := filepath.Join(base, "dest")
	if err := unpackTar(&archive, dest); err != nil {
		t.Fatalf("unpack: %v", err)
	}

	inodes := map[uint64]string{}
	for name, want := range map[string]string{"own": "own", "backups/own": "own", "planted": "outside"} {
		path := filepath.Join(dest, filepath.FromSlash(name))
		if got, err := os.ReadFile(path); err != nil || string(got) != want {
			t.Fatalf("%s reads %q (%v), want %q", name, got, err, want)
		}
		var st unix.Stat_t
		if err := unix.Lstat(path, &st); err != nil {
			t.Fatal(err)
		}
		if st.Nlink != 1 {
			t.Errorf("%s has %d names, want a file of its own", name, st.Nlink)
		}
		if other, dup := inodes[st.Ino]; dup {
			t.Errorf("%s shares an inode with %s", name, other)
		}
		inodes[st.Ino] = name
	}
}
