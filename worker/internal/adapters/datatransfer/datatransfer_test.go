package datatransfer

import (
	"archive/tar"
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

// tarOf builds an in-memory tar of {name: content}.
func tarOf(files map[string]string) []byte {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for name, content := range files {
		_ = tw.WriteHeader(&tar.Header{
			Name:     name,
			Typeflag: tar.TypeReg,
			Mode:     0o640,
			Size:     int64(len(content)),
		})
		_, _ = tw.Write([]byte(content))
	}
	_ = tw.Close()
	return buf.Bytes()
}

func TestHydrateUnpacksWorkingSet(t *testing.T) {
	body := tarOf(map[string]string{
		"server.properties": "motd=hi",
		"world/level.dat":   "data",
	})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer tok" {
			t.Errorf("auth header = %q, want Bearer tok", got)
		}
		w.Header().Set("X-Working-Set-Generation", "42")
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	dest := t.TempDir()
	c := New(srv.Client())
	gen, err := c.Hydrate(context.Background(), srv.URL, "tok", dest)
	if err != nil {
		t.Fatalf("Hydrate: %v", err)
	}
	if gen != 42 {
		t.Fatalf("generation = %d, want 42", gen)
	}

	got, err := os.ReadFile(filepath.Join(dest, "server.properties"))
	if err != nil || string(got) != "motd=hi" {
		t.Fatalf("server.properties = %q, %v", got, err)
	}
	got, err = os.ReadFile(filepath.Join(dest, "world", "level.dat"))
	if err != nil || string(got) != "data" {
		t.Fatalf("world/level.dat = %q, %v", got, err)
	}
}

func TestHydrateNoContentLeavesDestEmpty(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	dest := t.TempDir()
	c := New(srv.Client())
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("Hydrate: %v", err)
	}
	entries, _ := os.ReadDir(dest)
	if len(entries) != 0 {
		t.Fatalf("dest should be empty, got %d entries", len(entries))
	}
}

func TestHydrateRejectsPathEscape(t *testing.T) {
	// A member with a ../ escape must be refused, leaving nothing outside dest.
	body := tarOf(map[string]string{"../escape.txt": "pwned"})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	parent := t.TempDir()
	dest := filepath.Join(parent, "working")
	c := New(srv.Client())
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err == nil {
		t.Fatal("expected an error for the path-escape member")
	}
	if _, err := os.Stat(filepath.Join(parent, "escape.txt")); !os.IsNotExist(err) {
		t.Fatal("escape file was written outside dest")
	}
}

func TestHydrateRejectsSymlinkMember(t *testing.T) {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	_ = tw.WriteHeader(&tar.Header{
		Name:     "link",
		Typeflag: tar.TypeSymlink,
		Linkname: "/etc/passwd",
	})
	_ = tw.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(buf.Bytes())
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", t.TempDir()); err == nil {
		t.Fatal("expected an error for the symlink member")
	}
}

func TestSnapshotPacksAndUploadsWithContentLength(t *testing.T) {
	srcDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(srcDir, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "world", "level.dat"), []byte("w"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "server.properties"), []byte("p"), 0o640); err != nil {
		t.Fatal(err)
	}

	var received []byte
	var gotLen int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotLen = r.ContentLength
		received, _ = io.ReadAll(r.Body)
		w.Header().Set("X-Working-Set-Generation", "9")
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c := New(srv.Client())
	gen, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir)
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if gen != 9 {
		t.Fatalf("generation = %d, want 9", gen)
	}

	if gotLen <= 0 || gotLen != int64(len(received)) {
		t.Fatalf("Content-Length = %d, body len = %d (must match and be > 0)", gotLen, len(received))
	}

	// The uploaded tar must round-trip the working set.
	files := map[string]string{}
	tr := tar.NewReader(bytes.NewReader(received))
	for {
		h, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if h.Typeflag == tar.TypeReg {
			b, _ := io.ReadAll(tr)
			files[h.Name] = string(b)
		}
	}
	if files["server.properties"] != "p" || files["world/level.dat"] != "w" {
		t.Fatalf("uploaded tar = %v", files)
	}
}

func TestSnapshotStreamsLargeWorkingSetWithMatchingContentLength(t *testing.T) {
	// A multi-chunk working set must upload with a Content-Length that matches the
	// streamed byte count without the client buffering the whole tar in RAM. The
	// fake API counts the body as it arrives (never holding it all) and compares.
	srcDir := t.TempDir()
	const fileSize = 4 << 20 // 4 MiB, several HTTP chunks
	big := make([]byte, fileSize)
	for i := range big {
		big[i] = byte(i)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "world.dat"), big, 0o640); err != nil {
		t.Fatal(err)
	}

	var gotLen, counted int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotLen = r.ContentLength
		counted, _ = io.Copy(io.Discard, r.Body)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if gotLen <= fileSize {
		t.Fatalf("Content-Length = %d, want > %d (a tar of a %d-byte file)", gotLen, fileSize, fileSize)
	}
	if gotLen != counted {
		t.Fatalf("Content-Length = %d, streamed bytes = %d (must match)", gotLen, counted)
	}
}

func TestSnapshotRemovesSpoolFile(t *testing.T) {
	// The temp spool must not linger in the scratch root after a snapshot.
	srcDir := filepath.Join(t.TempDir(), "server")
	if err := os.MkdirAll(srcDir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "server.properties"), []byte("p"), 0o640); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	entries, err := os.ReadDir(filepath.Dir(srcDir))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Name() != "server" {
			t.Fatalf("leftover entry in scratch root: %q", e.Name())
		}
	}
}

func TestSnapshotEmptyDirUploadsEmptyTar(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", filepath.Join(t.TempDir(), "absent")); err != nil {
		t.Fatalf("Snapshot of absent dir: %v", err)
	}
}

func TestSnapshotPropagatesServerError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", t.TempDir()); err == nil {
		t.Fatal("expected an error for a 400 response")
	}
}
