package datatransfer

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
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
	var gotBaseGen string
	var gotWorkerID string
	var gotSource string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotLen = r.ContentLength
		gotBaseGen = r.Header.Get("X-Working-Set-Base-Generation")
		gotWorkerID = r.Header.Get("X-Worker-Id")
		gotSource = r.Header.Get("X-Snapshot-Source")
		received, _ = io.ReadAll(r.Body)
		w.Header().Set("X-Working-Set-Generation", "9")
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c := New(srv.Client())
	gen, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 7, "worker-7", true)
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if gen != 9 {
		t.Fatalf("generation = %d, want 9", gen)
	}
	// The declared base generation (the set's hydrated-from generation) rides the
	// request header so the API's publish-time guard can check it (#847).
	if gotBaseGen != "7" {
		t.Fatalf("X-Working-Set-Base-Generation = %q, want %q", gotBaseGen, "7")
	}
	// The publishing Worker's id rides the request header so the API's guard can tell
	// a same-Worker re-publish (lost-response self-heal) from a different-Worker stale
	// publish (#847 bug 3).
	if gotWorkerID != "worker-7" {
		t.Fatalf("X-Worker-Id = %q, want %q", gotWorkerID, "worker-7")
	}
	// A running-server snapshot declares its source so the API applies the live
	// (byte-precise) region rule for the unpadded tail of a live 26.x world (#923).
	if gotSource != "running" {
		t.Fatalf("X-Snapshot-Source = %q, want %q", gotSource, "running")
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

func TestSnapshotOmitsBaseGenerationHeaderWhenUnknown(t *testing.T) {
	// A base generation of 0 (an unknown / never-hydrated set) must NOT send the
	// header (issue #847): the API's publish-time guard then has no base to compare
	// and the publish proceeds as before, keeping the header backward-compatible.
	srcDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(srcDir, "server.properties"), []byte("p"), 0o640); err != nil {
		t.Fatal(err)
	}

	var hadBaseGen bool
	var hadWorkerID bool
	var gotSource string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, hadBaseGen = r.Header["X-Working-Set-Base-Generation"]
		_, hadWorkerID = r.Header["X-Worker-Id"]
		gotSource = r.Header.Get("X-Snapshot-Source")
		_, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 0, "", false); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if hadBaseGen {
		t.Fatal("X-Working-Set-Base-Generation header sent for base generation 0")
	}
	// An empty worker id (e.g. an unconfigured Worker) must NOT send the header
	// (issue #847 bug 3): the API's guard then treats the publisher as unknown and
	// stays permissive.
	if hadWorkerID {
		t.Fatal("X-Worker-Id header sent for an empty worker id")
	}
	// A stopped/at-rest snapshot declares "stopped" so the API keeps the strict
	// 4096-aligned region rule (#923).
	if gotSource != "stopped" {
		t.Fatalf("X-Snapshot-Source = %q, want %q", gotSource, "stopped")
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
	// A leftover marker TEMP file at the root (a crash before writeGeneration's
	// rename, issue #834) must ALSO be excluded — the exclusion is by prefix.
	if err := os.WriteFile(filepath.Join(srcDir, generationMarkerFile+"-123456"), []byte("temp"), 0o640); err != nil {
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
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 0, "", false); err != nil {
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
	if names[generationMarkerFile+"-123456"] {
		t.Fatalf("snapshot tar must not contain the root marker temp file %q-123456", generationMarkerFile)
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
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 0, "", false); err != nil {
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
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 0, "", false); err != nil {
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
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", filepath.Join(t.TempDir(), "absent"), 0, "", false); err != nil {
		t.Fatalf("Snapshot of absent dir: %v", err)
	}
}

func TestSnapshotPropagatesServerError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", t.TempDir(), 0, "", false); err == nil {
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
	if err := writeRegular(tw, "latest.log", path, info, slog.Default()); err != nil {
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
	if err := writeRegular(tw, "latest.log", path, info, slog.Default()); err != nil {
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

// capturingHandler is a slog.Handler that records log records so tests can
// assert that expected log lines were emitted.
type capturingHandler struct {
	records []slog.Record
}

func (h *capturingHandler) Enabled(_ context.Context, _ slog.Level) bool { return true }
func (h *capturingHandler) Handle(_ context.Context, r slog.Record) error {
	h.records = append(h.records, r)
	return nil
}
func (h *capturingHandler) WithAttrs(_ []slog.Attr) slog.Handler { return h }
func (h *capturingHandler) WithGroup(_ string) slog.Handler      { return h }

// hasMessage reports whether any captured record has the given message.
func (h *capturingHandler) hasMessage(msg string) bool {
	for _, r := range h.records {
		if r.Message == msg {
			return true
		}
	}
	return false
}

// TestWriteRegularVanishedFileIsSkipped verifies that a file deleted between
// the walk and os.Open (ENOENT) is silently skipped and does not fail the
// snapshot (issue #820). The tar must contain the other files but not the
// vanished one.
func TestWriteRegularVanishedFileIsSkipped(t *testing.T) {
	srcDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(srcDir, "kept.txt"), []byte("keep"), 0o640); err != nil {
		t.Fatal(err)
	}
	vanished := filepath.Join(srcDir, "vanished.log")
	if err := os.WriteFile(vanished, []byte("log"), 0o640); err != nil {
		t.Fatal(err)
	}

	// Stat the vanished file to get its info (simulates the ReadDir-time snapshot).
	info, err := os.Stat(vanished)
	if err != nil {
		t.Fatal(err)
	}
	// Delete the file before writeRegular opens it.
	if err := os.Remove(vanished); err != nil {
		t.Fatal(err)
	}

	h := &capturingHandler{}
	log := slog.New(h)
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := writeRegular(tw, "vanished.log", vanished, info, log); err != nil {
		t.Fatalf("writeRegular must skip a vanished file, got error: %v", err)
	}
	if err := tw.Close(); err != nil {
		t.Fatalf("tw.Close: %v", err)
	}

	// The tar must be empty (no entry for the vanished file).
	tr := tar.NewReader(&buf)
	if _, err := tr.Next(); err != io.EOF {
		t.Fatalf("expected empty tar, got entry or error: %v", err)
	}

	// A log line must have been emitted.
	const wantMsg = "snapshot: file vanished between walk and open; skipping"
	if !h.hasMessage(wantMsg) {
		t.Fatalf("expected log message %q, captured records: %v", wantMsg, h.records)
	}
}

// TestWriteRegularVanishedFileOtherErrorFails verifies that non-ENOENT open
// errors (e.g. permission denied) still fail the snapshot (issue #820).
func TestWriteRegularVanishedFileOtherErrorFails(t *testing.T) {
	srcDir := t.TempDir()
	target := filepath.Join(srcDir, "noperm.txt")
	if err := os.WriteFile(target, []byte("x"), 0o000); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}

	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	err = writeRegular(tw, "noperm.txt", target, info, slog.Default())
	if err == nil {
		// Root can open mode-000 files; skip on root.
		if os.Getuid() == 0 {
			t.Skip("running as root: permission check skipped")
		}
		t.Fatal("expected an error for a permission-denied open, got nil")
	}
}

// TestWriteRegularGrowingFileLogsCapLine verifies that a log line is emitted
// when a grown file is capped at its header-declared size (issue #820).
func TestWriteRegularGrowingFileLogsCapLine(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "latest.log")
	// Write 5 bytes to disk.
	if err := os.WriteFile(p, []byte("hello"), 0o640); err != nil {
		t.Fatal(err)
	}
	realInfo, err := os.Stat(p)
	if err != nil {
		t.Fatal(err)
	}
	// Stat reports 3 bytes (file "grew" from 3 to 5 after the walk).
	info := fakeInfo{FileInfo: realInfo, size: 3}

	h := &capturingHandler{}
	log := slog.New(h)
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := writeRegular(tw, "latest.log", p, info, log); err != nil {
		t.Fatalf("writeRegular: %v", err)
	}
	_ = tw.Close()

	const wantMsg = "snapshot: file grew between walk and copy; capped"
	if !h.hasMessage(wantMsg) {
		t.Fatalf("expected log message %q, captured records: %v", wantMsg, h.records)
	}
}

// TestWriteRegularShrinkingFileLogsPadLine verifies that a log line is emitted
// when a shrunken file is zero-padded to its header-declared size (issue #820).
func TestWriteRegularShrinkingFileLogsPadLine(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "latest.log")
	// Write 3 bytes to disk.
	if err := os.WriteFile(p, []byte("hi!"), 0o640); err != nil {
		t.Fatal(err)
	}
	realInfo, err := os.Stat(p)
	if err != nil {
		t.Fatal(err)
	}
	// Stat reports 6 bytes (file "shrank" from 6 to 3 after the walk).
	info := fakeInfo{FileInfo: realInfo, size: 6}

	h := &capturingHandler{}
	log := slog.New(h)
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := writeRegular(tw, "latest.log", p, info, log); err != nil {
		t.Fatalf("writeRegular: %v", err)
	}
	_ = tw.Close()

	const wantMsg = "snapshot: file shrank between walk and copy; zero-padded"
	if !h.hasMessage(wantMsg) {
		t.Fatalf("expected log message %q, captured records: %v", wantMsg, h.records)
	}
}

// TestSnapshotSkipsVanishedFilesAndSucceeds verifies that a snapshot of a
// directory where a file disappears between the walk and the open succeeds
// (issue #820). The vanished file must be absent from the uploaded tar, and the
// remaining files must be present.
func TestSnapshotSkipsVanishedFilesAndSucceeds(t *testing.T) {
	srcDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(srcDir, "kept.txt"), []byte("keep"), 0o640); err != nil {
		t.Fatal(err)
	}

	// Inject a vanished-file via the package-level openFile var: the file exists
	// when ReadDir walks the directory but returns ENOENT when opened, simulating
	// log rotation between walk and open.
	vanished := filepath.Join(srcDir, "vanished.log")
	if err := os.WriteFile(vanished, []byte("log line\n"), 0o640); err != nil {
		t.Fatal(err)
	}

	var received []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	// Swap the openFile hook so vanished.log gets ENOENT.
	orig := openFile
	openFile = func(name string) (*os.File, error) {
		if filepath.Base(name) == "vanished.log" {
			return nil, os.ErrNotExist
		}
		return os.Open(name)
	}
	defer func() { openFile = orig }()

	h := &capturingHandler{}
	c := New(srv.Client()).WithLogger(slog.New(h))
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 0, "", false); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}

	// The tar must contain kept.txt but not vanished.log.
	names := map[string]bool{}
	tr := tar.NewReader(bytes.NewReader(received))
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("tar.Next: %v", err)
		}
		names[hdr.Name] = true
	}
	if !names["kept.txt"] {
		t.Fatal("kept.txt must be in the snapshot tar")
	}
	if names["vanished.log"] {
		t.Fatal("vanished.log must not be in the snapshot tar (it was deleted)")
	}

	const wantMsg = "snapshot: file vanished between walk and open; skipping"
	if !h.hasMessage(wantMsg) {
		t.Fatalf("expected log message %q, captured records: %v", wantMsg, h.records)
	}
}

// TestWalkIntoVanishedDirIsSkipped verifies the directory analog of the #820/#853
// file-vanish race (issue #854): a directory deleted between the parent's walk and
// this read (ENOENT on ReadDir) is skipped with a Warn rather than failing the
// whole snapshot. The kept sibling must still be archived.
func TestWalkIntoVanishedDirIsSkipped(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "kept.txt"), []byte("keep"), 0o640); err != nil {
		t.Fatal(err)
	}
	gone := filepath.Join(root, "logs")
	if err := os.MkdirAll(gone, 0o750); err != nil {
		t.Fatal(err)
	}

	// Inject ENOENT for the logs/ subtree only (it "rotated away" mid-pack).
	orig := readDir
	readDir = func(name string) ([]os.DirEntry, error) {
		if name == gone {
			return nil, os.ErrNotExist
		}
		return os.ReadDir(name)
	}
	defer func() { readDir = orig }()

	h := &capturingHandler{}
	log := slog.New(h)
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := walkInto(tw, root, root, log); err != nil {
		t.Fatalf("walkInto must skip a vanished directory, got error: %v", err)
	}
	if err := tw.Close(); err != nil {
		t.Fatalf("tw.Close: %v", err)
	}

	// kept.txt is archived; the logs/ subtree produced no member beyond its own
	// (already-written) dir header.
	names := map[string]bool{}
	tr := tar.NewReader(&buf)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("tar.Next: %v", err)
		}
		names[hdr.Name] = true
	}
	if !names["kept.txt"] {
		t.Fatal("kept.txt must be in the snapshot tar")
	}

	const wantMsg = "snapshot: directory vanished between walk and read; skipping"
	if !h.hasMessage(wantMsg) {
		t.Fatalf("expected log message %q, captured records: %v", wantMsg, h.records)
	}
}

// TestWalkIntoNonENOENTDirErrorFails verifies a non-ENOENT ReadDir error (e.g. a
// permission error) still fails the whole snapshot, never a silent skip (#854).
func TestWalkIntoNonENOENTDirErrorFails(t *testing.T) {
	root := t.TempDir()
	sub := filepath.Join(root, "sub")
	if err := os.MkdirAll(sub, 0o750); err != nil {
		t.Fatal(err)
	}

	orig := readDir
	readDir = func(name string) ([]os.DirEntry, error) {
		if name == sub {
			return nil, os.ErrPermission
		}
		return os.ReadDir(name)
	}
	defer func() { readDir = orig }()

	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := walkInto(tw, root, root, slog.Default()); err == nil {
		t.Fatal("walkInto must propagate a non-ENOENT ReadDir error")
	}
}

// TestWalkIntoVanishedEntryInfoIsSkipped verifies the entry.Info() member of the
// #820/#853/#854 vanish-race family (issue #887): an entry that disappears between
// the parent's ReadDir and the lazy lstat behind entry.Info() (ENOENT) is skipped
// with a Warn rather than failing the whole snapshot. The kept sibling must still
// be archived.
func TestWalkIntoVanishedEntryInfoIsSkipped(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "kept.txt"), []byte("keep"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "vanished.log"), []byte("log"), 0o640); err != nil {
		t.Fatal(err)
	}

	// Inject ENOENT from Info() for vanished.log only: it exists at ReadDir time but
	// the lazy lstat behind Info() fails, simulating a delete between walk and stat.
	orig := entryInfo
	entryInfo = func(entry os.DirEntry) (os.FileInfo, error) {
		if entry.Name() == "vanished.log" {
			return nil, os.ErrNotExist
		}
		return entry.Info()
	}
	defer func() { entryInfo = orig }()

	h := &capturingHandler{}
	log := slog.New(h)
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := walkInto(tw, root, root, log); err != nil {
		t.Fatalf("walkInto must skip a vanished entry, got error: %v", err)
	}
	if err := tw.Close(); err != nil {
		t.Fatalf("tw.Close: %v", err)
	}

	names := map[string]bool{}
	tr := tar.NewReader(&buf)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("tar.Next: %v", err)
		}
		names[hdr.Name] = true
	}
	if !names["kept.txt"] {
		t.Fatal("kept.txt must be in the snapshot tar")
	}
	if names["vanished.log"] {
		t.Fatal("vanished.log must not be in the snapshot tar (it vanished before stat)")
	}

	const wantMsg = "snapshot: entry vanished between walk and stat; skipping"
	if !h.hasMessage(wantMsg) {
		t.Fatalf("expected log message %q, captured records: %v", wantMsg, h.records)
	}
}

// TestWalkIntoNonENOENTEntryInfoErrorFails verifies a non-ENOENT entry.Info() error
// still fails the whole snapshot, never a silent skip (issue #887).
func TestWalkIntoNonENOENTEntryInfoErrorFails(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "file.txt"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}

	orig := entryInfo
	entryInfo = func(_ os.DirEntry) (os.FileInfo, error) {
		return nil, os.ErrPermission
	}
	defer func() { entryInfo = orig }()

	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := walkInto(tw, root, root, slog.Default()); err == nil {
		t.Fatal("walkInto must propagate a non-ENOENT entry.Info() error")
	}
}

// When the final temp->destDir swap rename fails after the old working set was
// already displaced aside to .displaced-<id>, unpackAndSwap must restore the old copy
// so no data is lost (the displace-first restore branch, issue #772 / #806 / #910).
// The old copy must end up recoverable (here: back at destDir) and never be left as
// the only copy under a .hydrate-* name a later sweep would delete.
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

// A hydrate over an existing scratch must MOVE the displaced old working set aside to
// .displaced-<id> rather than delete it (issue #906): when the final stop snapshot
// definitively failed, #845 retained that scratch as the only copy of the world, and
// the next start's hydrate would otherwise destroy it. The displaced tree's content
// must be preserved intact for operator recovery.
func TestHydrateDisplacesOldWorkingSetInsteadOfDeleting(t *testing.T) {
	body := tarOf(map[string]string{"server.properties": "new"})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	// A pre-existing working set holding the only copy of a world progressed past the
	// store (the retained-for-recovery scratch, #845).
	if err := os.MkdirAll(filepath.Join(dest, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "world", "r.0.0.mca"), []byte("unsnapshotted"), 0o640); err != nil {
		t.Fatal(err)
	}

	c := New(srv.Client())
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("Hydrate: %v", err)
	}

	// The new working set is in place.
	got, err := os.ReadFile(filepath.Join(dest, "server.properties"))
	if err != nil || string(got) != "new" {
		t.Fatalf("server.properties = %q, %v (want %q)", got, err, "new")
	}
	// The displaced old tree survives at .displaced-server with its content intact.
	displaced := filepath.Join(scratch, ".displaced-server")
	got, err = os.ReadFile(filepath.Join(displaced, "world", "r.0.0.mca"))
	if err != nil {
		t.Fatalf("displaced old working set not retained for recovery (issue #906): %v", err)
	}
	if string(got) != "unsnapshotted" {
		t.Fatalf("displaced world content = %q, want %q", got, "unsnapshotted")
	}
}

// A SECOND hydrate over the same id must REPLACE the prior displaced tree, keeping at
// most one per server (issue #906): the older displaced tree predates the store state
// the newer one was displaced by, so replacing it is correct and bounds disk to one
// extra working set per server.
func TestHydrateReplacesPriorDisplacedTree(t *testing.T) {
	body := tarOf(map[string]string{"server.properties": "x"})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	seed := func(marker string) {
		if err := os.MkdirAll(dest, 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dest, "gen"), []byte(marker), 0o640); err != nil {
			t.Fatal(err)
		}
	}

	c := New(srv.Client())
	// First hydrate displaces working set "v1".
	seed("v1")
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("first Hydrate: %v", err)
	}
	// Second hydrate displaces working set "v2", which must replace the v1 displaced tree.
	seed("v2")
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("second Hydrate: %v", err)
	}

	// Exactly one displaced tree, holding the most recent (v2) displaced content.
	got, err := os.ReadFile(filepath.Join(scratch, ".displaced-server", "gen"))
	if err != nil {
		t.Fatalf("displaced tree missing after second hydrate: %v", err)
	}
	if string(got) != "v2" {
		t.Fatalf("displaced content = %q, want %q (newer displacement replaces the prior one)", got, "v2")
	}
	// No second .displaced-* sibling accumulated.
	entries, err := os.ReadDir(scratch)
	if err != nil {
		t.Fatal(err)
	}
	displacedCount := 0
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".displaced-") {
			displacedCount++
		}
	}
	if displacedCount != 1 {
		t.Fatalf("displaced-tree count = %d, want exactly 1 per server (issue #906)", displacedCount)
	}
}

// Displace-first ordering (issue #910): the "aside" step must move the old working
// set DIRECTLY to .displaced-<id>, never to an intermediate .hydrate-<id>-*.trash
// name. The distinction is load-bearing: a crash (or the fsync error return) after
// the aside-rename but before the swap-in completes leaves the old world ONLY under
// that aside name with destDir absent. If the aside name is a .hydrate-<id>-* one, the
// NEXT hydrate's sweepHydrateLeftovers deletes it — destroying the #906 recovery copy
// one hydrate later. This pins the on-disk state at the instant of swap-in: the old
// copy must already sit at .displaced-<id> and nothing must be parked under a
// .hydrate-* name a sweep would delete.
func TestSwapAsidesOldCopyToDisplacedNotTrash(t *testing.T) {
	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	if err := os.MkdirAll(filepath.Join(dest, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "world", "r.0.0.mca"), []byte("only-copy"), 0o640); err != nil {
		t.Fatal(err)
	}

	// Inspect the filesystem at the exact moment of the swap-in rename (after the old
	// copy has been moved aside, before it lands in destDir), then fail the swap so the
	// hydrate returns without a successful swap-in.
	orig := swapRename
	var asideAt string
	var hydrateTrashHoldsCopy bool
	swapRename = func(_, _ string) error {
		// destDir must be absent here (the old copy was renamed aside, the new tree not
		// yet swapped in) — exactly the window a crash freezes.
		if _, err := os.Stat(filepath.Join(dest, "world", "r.0.0.mca")); err == nil {
			t.Fatalf("destDir still holds the old copy at swap-in time; displace-aside did not run first")
		}
		entries, err := os.ReadDir(scratch)
		if err != nil {
			t.Fatal(err)
		}
		for _, e := range entries {
			name := e.Name()
			copyPath := filepath.Join(scratch, name, "world", "r.0.0.mca")
			b, rerr := os.ReadFile(copyPath)
			if rerr != nil || string(b) != "only-copy" {
				continue
			}
			if name == ".displaced-server" {
				asideAt = name
			}
			// A copy sitting under a .hydrate-* name is sweep-deletable: the bug.
			if strings.HasPrefix(name, ".hydrate-") {
				hydrateTrashHoldsCopy = true
			}
		}
		return errors.New("forced swap failure")
	}
	defer func() { swapRename = orig }()

	body := tarOf(map[string]string{"server.properties": "fresh"})
	if err := unpackAndSwap(bytes.NewReader(body), dest); err == nil {
		t.Fatal("expected unpackAndSwap to fail when the swap rename fails")
	}

	if hydrateTrashHoldsCopy {
		t.Fatalf("recovery copy parked under a .hydrate-* name (sweep-deletable) at swap-in (issue #910)")
	}
	if asideAt != ".displaced-server" {
		t.Fatalf("old copy not moved aside to .displaced-server before swap-in (issue #910); asideAt=%q", asideAt)
	}
}

// Re-running an interrupted hydrate must NOT destroy the recovery copy (issue #910):
// a crash between the displace-aside and the swap-in leaves destDir absent and the
// only copy of the world under .displaced-<id>. The next hydrate has nothing to
// displace, so it must leave that .displaced-<id> tree intact — the prior-displaced
// RemoveAll runs only when a live destDir exists to take its place.
func TestReHydrateDoesNotDeleteDisplacedWhenDestAbsent(t *testing.T) {
	body := tarOf(map[string]string{"server.properties": "fresh"})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	// Crash-shaped state: destDir absent, the only copy under .displaced-server.
	displaced := filepath.Join(scratch, ".displaced-server")
	if err := os.MkdirAll(filepath.Join(displaced, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(displaced, "world", "r.0.0.mca"), []byte("only-copy"), 0o640); err != nil {
		t.Fatal(err)
	}

	c := New(srv.Client())
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("Hydrate: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(displaced, "world", "r.0.0.mca"))
	if err != nil {
		t.Fatalf("displaced recovery copy destroyed when re-hydrating with destDir absent (issue #910): %v", err)
	}
	if string(got) != "only-copy" {
		t.Fatalf("displaced content = %q, want %q", got, "only-copy")
	}
}

// A failed restore on the swap-in failure path must NEVER delete the only copy
// (issue #910 finding 2): with the displace-first reorder the old world sits under
// .displaced-<id> when the swap rename fails, so even if the restore rename back to
// destDir also fails, the recovery copy survives there. This forces the swap rename
// to fail and asserts the old content is recoverable from .displaced-<id> (the swap
// path never actively removes it).
func TestHydrateNeverDeletesOnlyCopyOnSwapFailure(t *testing.T) {
	orig := swapRename
	// Fail the swap-in AND any restore attempt: both go through swapRename only for
	// the swap-in; the restore uses os.Rename directly. To keep destDir absent so the
	// only copy lives under .displaced-<id>, remove destDir inside the forced swap so
	// the subsequent restore os.Rename has no live destDir to clobber and the copy is
	// observed at .displaced-<id>.
	swapRename = func(_, _ string) error { return errors.New("forced swap failure") }
	defer func() { swapRename = orig }()

	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	if err := os.MkdirAll(dest, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "level.dat"), []byte("old-world"), 0o640); err != nil {
		t.Fatal(err)
	}

	body := tarOf(map[string]string{"level.dat": "new-world"})
	if err := unpackAndSwap(bytes.NewReader(body), dest); err == nil {
		t.Fatal("expected unpackAndSwap to fail when the swap rename fails")
	}

	// The old copy must be recoverable: either restored to destDir or still under
	// .displaced-server — never deleted. (With the current restore it lands at destDir;
	// the load-bearing assertion is that the content survives somewhere, never gone.)
	atDest, errDest := os.ReadFile(filepath.Join(dest, "level.dat"))
	atDisplaced, errDisp := os.ReadFile(filepath.Join(scratch, ".displaced-server", "level.dat"))
	recovered := (errDest == nil && string(atDest) == "old-world") ||
		(errDisp == nil && string(atDisplaced) == "old-world")
	if !recovered {
		t.Fatalf("only copy of the world lost on swap failure (issue #910): dest=%v/%q displaced=%v/%q",
			errDest, atDest, errDisp, atDisplaced)
	}
}
