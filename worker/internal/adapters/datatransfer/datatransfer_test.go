package datatransfer

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
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

func TestHydrateReplacesStaleWorkingSet(t *testing.T) {
	// Hydrate must REPLACE the dest's contents, not merge: a file present in the
	// stale working set but absent from the served tar must be gone afterwards
	// (the A->B->A stale-generation case, issue #772). A merge would leave the
	// stale file behind, producing an internally inconsistent mixed-generation
	// world that region fsck cannot detect.
	body := tarOf(map[string]string{
		"server.properties": "new",
		"world/level.dat":   "new-world",
	})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	// A pre-existing (stale) working set: a file the new tar does NOT carry, plus
	// an old copy of one it does.
	dest := filepath.Join(t.TempDir(), "server")
	if err := os.MkdirAll(filepath.Join(dest, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "stale-plugin.jar"), []byte("old"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "world", "old-region.mca"), []byte("old"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "server.properties"), []byte("old"), 0o640); err != nil {
		t.Fatal(err)
	}

	c := New(srv.Client())
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("Hydrate: %v", err)
	}

	// The stale, upstream-deleted files must be gone.
	if _, err := os.Stat(filepath.Join(dest, "stale-plugin.jar")); !os.IsNotExist(err) {
		t.Fatal("stale-plugin.jar survived the hydrate (merge, not replace)")
	}
	if _, err := os.Stat(filepath.Join(dest, "world", "old-region.mca")); !os.IsNotExist(err) {
		t.Fatal("world/old-region.mca survived the hydrate (merge, not replace)")
	}
	// The served working set must be present and current.
	got, err := os.ReadFile(filepath.Join(dest, "server.properties"))
	if err != nil || string(got) != "new" {
		t.Fatalf("server.properties = %q, %v (want %q)", got, err, "new")
	}
	got, err = os.ReadFile(filepath.Join(dest, "world", "level.dat"))
	if err != nil || string(got) != "new-world" {
		t.Fatalf("world/level.dat = %q, %v (want %q)", got, err, "new-world")
	}
}

func TestHydrateDoesNotFollowPreexistingSymlink(t *testing.T) {
	// A pre-existing symlink in the working set at a path a tar member also names
	// must NOT be followed: hydrating into a brand-new tree means the planted link
	// is never traversed, so the link's target is left untouched (issue #772).
	body := tarOf(map[string]string{"server.properties": "from-tar"})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	tmp := t.TempDir()
	// The out-of-sandbox file a malicious symlink would target.
	outside := filepath.Join(tmp, "outside-secret")
	if err := os.WriteFile(outside, []byte("secret"), 0o600); err != nil {
		t.Fatal(err)
	}
	// The working set carries a planted symlink at the path the tar will write.
	dest := filepath.Join(tmp, "server")
	if err := os.MkdirAll(dest, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(dest, "server.properties")); err != nil {
		t.Fatal(err)
	}

	c := New(srv.Client())
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("Hydrate: %v", err)
	}

	// The symlink target outside the sandbox must be untouched.
	got, err := os.ReadFile(outside)
	if err != nil || string(got) != "secret" {
		t.Fatalf("outside target = %q, %v (want %q, must not be written through)", got, err, "secret")
	}
	// The dest now holds the served file as a plain regular file.
	info, err := os.Lstat(filepath.Join(dest, "server.properties"))
	if err != nil {
		t.Fatalf("Lstat server.properties: %v", err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		t.Fatal("server.properties is still a symlink after hydrate")
	}
	got, err = os.ReadFile(filepath.Join(dest, "server.properties"))
	if err != nil || string(got) != "from-tar" {
		t.Fatalf("server.properties = %q, %v (want %q)", got, err, "from-tar")
	}
}

func TestHydrateLeavesNoTempSiblingsInScratch(t *testing.T) {
	// The temp/trash dirs the swap uses live in the scratch root next to dest; a
	// successful hydrate must clean them all up so ScanHeldServers does not later
	// see bogus held-server entries (issue #772, scratchscan.go interplay).
	body := tarOf(map[string]string{"server.properties": "x"})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	c := New(srv.Client())
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("Hydrate: %v", err)
	}

	entries, err := os.ReadDir(scratch)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Name() != "server" {
			t.Fatalf("leftover entry in scratch root after hydrate: %q", e.Name())
		}
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

func TestSnapshotExcludesGenerationMarker(t *testing.T) {
	// The Worker-private generation marker at the scratch root must NOT be packed
	// into the snapshot (issue #763): it is Worker-private state that would
	// otherwise land in the authoritative stored working set and be re-hydrated to
	// other Workers / the live Minecraft dir. A same-named file deeper in the tree
	// is part of the legitimate world and must still be packed.
	srcDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(srcDir, generationMarkerFile), []byte("7"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "server.properties"), []byte("p"), 0o640); err != nil {
		t.Fatal(err)
	}
	// A file with the marker's name but inside a sub-tree is NOT the marker.
	nested := filepath.Join(srcDir, "world")
	if err := os.MkdirAll(nested, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(nested, generationMarkerFile), []byte("nested"), 0o640); err != nil {
		t.Fatal(err)
	}

	var received []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}

	names := map[string]bool{}
	tr := tar.NewReader(bytes.NewReader(received))
	for {
		h, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		names[h.Name] = true
	}
	if names[generationMarkerFile] {
		t.Fatalf("snapshot tar must not contain the root generation marker %q", generationMarkerFile)
	}
	if !names["server.properties"] {
		t.Fatal("snapshot tar must contain server.properties")
	}
	if !names["world/"+generationMarkerFile] {
		t.Fatalf("snapshot tar must contain the nested %q (not the root marker)", generationMarkerFile)
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

func TestSweepSnapshotSpoolsRemovesLeftoverSpools(t *testing.T) {
	// A crash mid-snapshot leaks snapshot-*.tar in the scratch root; the startup
	// sweep must reclaim them while leaving server working-set dirs and unrelated
	// files untouched (issue #787).
	scratch := t.TempDir()
	leaked := []string{"snapshot-123.tar", "snapshot-abc.tar"}
	for _, name := range leaked {
		if err := os.WriteFile(filepath.Join(scratch, name), []byte("x"), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	// A server working set (dir) and an unrelated file must survive.
	if err := os.MkdirAll(filepath.Join(scratch, "s1"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(scratch, "snapshot-notatar.txt"), []byte("y"), 0o640); err != nil {
		t.Fatal(err)
	}

	SweepSnapshotSpools(scratch)

	for _, name := range leaked {
		if _, err := os.Stat(filepath.Join(scratch, name)); !os.IsNotExist(err) {
			t.Fatalf("spool %q survived the sweep: stat err = %v", name, err)
		}
	}
	if _, err := os.Stat(filepath.Join(scratch, "s1")); err != nil {
		t.Fatalf("server dir removed by sweep: %v", err)
	}
	if _, err := os.Stat(filepath.Join(scratch, "snapshot-notatar.txt")); err != nil {
		t.Fatalf("non-.tar file removed by sweep: %v", err)
	}
}

func TestSweepSnapshotSpoolsMissingRootIsNoOp(t *testing.T) {
	// A worker with no scratch root yet must not panic or error (best-effort).
	SweepSnapshotSpools(filepath.Join(t.TempDir(), "absent"))
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

// fakeInfo wraps a real os.FileInfo but overrides Size() so we can simulate a
// file that grew or shrank between the ReadDir stat and the actual copy.
type fakeInfo struct {
	os.FileInfo
	size int64
}

func (f fakeInfo) Size() int64 { return f.size }

// TestWriteRegularGrowingFile verifies that a file that grows between the stat
// and the copy does not cause ErrWriteTooLong: the tar entry must be exactly
// the header-declared size and the archive must untar cleanly.
func TestWriteRegularGrowingFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "latest.log")
	// Write 5 bytes to disk.
	original := []byte("hello")
	if err := os.WriteFile(path, original, 0o640); err != nil {
		t.Fatal(err)
	}
	// Stat reports only 3 bytes (simulating the ReadDir-time snapshot before the
	// file grew to 5 bytes).
	realInfo, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	info := fakeInfo{FileInfo: realInfo, size: 3}

	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := writeRegular(tw, "latest.log", path, info); err != nil {
		t.Fatalf("writeRegular with grown file: %v", err)
	}
	if err := tw.Close(); err != nil {
		t.Fatalf("tw.Close with grown file: %v", err)
	}

	// The archive must untar cleanly and the entry must be exactly 3 bytes.
	tr := tar.NewReader(&buf)
	h, err := tr.Next()
	if err != nil {
		t.Fatalf("tar.Next: %v", err)
	}
	if h.Size != 3 {
		t.Fatalf("header.Size = %d, want 3", h.Size)
	}
	content, err := io.ReadAll(tr)
	if err != nil {
		t.Fatalf("read entry: %v", err)
	}
	if int64(len(content)) != h.Size {
		t.Fatalf("entry bytes = %d, want %d", len(content), h.Size)
	}
	// Content must be the first 3 bytes of the file (the file grew, we capped).
	if string(content) != "hel" {
		t.Fatalf("entry content = %q, want %q", string(content), "hel")
	}
}

// TestWriteRegularShrinkingFile verifies that a file that shrinks between the
// stat and the copy does not leave the tar in an inconsistent state: the entry
// is zero-padded to the header-declared size and the archive untars cleanly.
func TestWriteRegularShrinkingFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "latest.log")
	// Write 3 bytes to disk.
	if err := os.WriteFile(path, []byte("hi!"), 0o640); err != nil {
		t.Fatal(err)
	}
	// Stat reports 6 bytes (simulating the ReadDir-time snapshot before the
	// file shrank from 6 bytes to 3 bytes).
	realInfo, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	info := fakeInfo{FileInfo: realInfo, size: 6}

	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := writeRegular(tw, "latest.log", path, info); err != nil {
		t.Fatalf("writeRegular with shrunk file: %v", err)
	}
	if err := tw.Close(); err != nil {
		t.Fatalf("tw.Close with shrunk file: %v", err)
	}

	// The archive must untar cleanly and the entry must be exactly 6 bytes.
	tr := tar.NewReader(&buf)
	h, err := tr.Next()
	if err != nil {
		t.Fatalf("tar.Next: %v", err)
	}
	if h.Size != 6 {
		t.Fatalf("header.Size = %d, want 6", h.Size)
	}
	content, err := io.ReadAll(tr)
	if err != nil {
		t.Fatalf("read entry: %v", err)
	}
	if int64(len(content)) != h.Size {
		t.Fatalf("entry bytes = %d, want %d", len(content), h.Size)
	}
	// First 3 bytes are the file content; last 3 are zero-padding.
	if string(content) != "hi!\x00\x00\x00" {
		t.Fatalf("entry content = %q, want %q", content, "hi!\x00\x00\x00")
	}
}

// sweepHydrateLeftovers reclaims this id's .hydrate-<id>-* temp/trash siblings a
// crashed hydrate left behind, and touches nothing else (issue #806).
func TestSweepHydrateLeftovers(t *testing.T) {
	scratch := t.TempDir()
	// A stale leftover for "server" from a crashed hydrate.
	stale := filepath.Join(scratch, ".hydrate-server-stale")
	if err := os.MkdirAll(stale, 0o750); err != nil {
		t.Fatal(err)
	}
	// The live working dir and another server's leftover must be retained: the sweep
	// is an exact-prefix match for the given id only.
	live := filepath.Join(scratch, "server")
	if err := os.MkdirAll(live, 0o750); err != nil {
		t.Fatal(err)
	}
	other := filepath.Join(scratch, ".hydrate-other-stale")
	if err := os.MkdirAll(other, 0o750); err != nil {
		t.Fatal(err)
	}

	sweepHydrateLeftovers(scratch, "server")

	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatalf(".hydrate-server-stale not removed: stat err = %v", err)
	}
	if _, err := os.Stat(live); err != nil {
		t.Fatalf("live working dir wrongly removed: %v", err)
	}
	if _, err := os.Stat(other); err != nil {
		t.Fatalf("another server's leftover wrongly removed: %v", err)
	}
}

// When the final temp->destDir swap rename fails after the old working set was
// already moved aside to trash, unpackAndSwap must restore the old copy so no data
// is lost (the crash-safety restore branch, issue #772 / #806).
func TestHydrateRestoresOldCopyWhenSwapRenameFails(t *testing.T) {
	orig := swapRename
	swapRename = func(_, _ string) error { return errors.New("forced swap failure") }
	defer func() { swapRename = orig }()

	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	// A pre-existing (old) working set the swap must not lose.
	if err := os.MkdirAll(dest, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "level.dat"), []byte("old-world"), 0o640); err != nil {
		t.Fatal(err)
	}

	body := tarOf(map[string]string{"level.dat": "new-world"})
	err := unpackAndSwap(bytes.NewReader(body), dest)
	if err == nil {
		t.Fatal("expected unpackAndSwap to fail when the swap rename fails")
	}

	// The old copy must be back at destDir (restored from trash) with its content.
	got, err := os.ReadFile(filepath.Join(dest, "level.dat"))
	if err != nil {
		t.Fatalf("old working set not restored to destDir: %v", err)
	}
	if string(got) != "old-world" {
		t.Fatalf("destDir/level.dat = %q, want %q (old copy)", got, "old-world")
	}
	// No .hydrate-* leftovers should remain to leak disk.
	entries, err := os.ReadDir(scratch)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Name() != "server" {
			t.Fatalf("leftover entry in scratch root after failed swap: %q", e.Name())
		}
	}
}
