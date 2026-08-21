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
	"strconv"
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
	var hadSource bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotLen = r.ContentLength
		gotBaseGen = r.Header.Get("X-Working-Set-Base-Generation")
		gotWorkerID = r.Header.Get("X-Worker-Id")
		_, hadSource = r.Header["X-Snapshot-Source"]
		received, _ = io.ReadAll(r.Body)
		w.Header().Set("X-Working-Set-Generation", "9")
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c := New(srv.Client())
	gen, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 7, "worker-7")
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
	// The snapshot-source mode header is gone (#927: one region rule set, no
	// source-keyed split), so the Worker never sends it.
	if hadSource {
		t.Fatal("X-Snapshot-Source header sent; the mode split was removed (#927)")
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
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, hadBaseGen = r.Header["X-Working-Set-Base-Generation"]
		_, hadWorkerID = r.Header["X-Worker-Id"]
		_, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 0, ""); err != nil {
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
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 0, ""); err != nil {
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
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 0, ""); err != nil {
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
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 0, ""); err != nil {
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

// PackSnapshot creates a tar spool in the scratch root (srcDir's parent) that
// contains the working set, and its cleanup function removes the spool (issue #1710).
func TestPackSnapshotSpoolsToScratchRoot(t *testing.T) {
	scratch := t.TempDir()
	srcDir := filepath.Join(scratch, "server")
	if err := os.MkdirAll(filepath.Join(srcDir, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "server.properties"), []byte("p"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "world", "level.dat"), []byte("w"), 0o640); err != nil {
		t.Fatal(err)
	}

	c := New(nil) // no http.Client needed for packing
	spoolPath, cleanup, err := c.PackSnapshot(context.Background(), srcDir)
	if err != nil {
		t.Fatalf("PackSnapshot: %v", err)
	}
	// The spool must exist in the scratch root with the expected prefix.
	if filepath.Dir(spoolPath) != scratch {
		t.Fatalf("spool dir = %q, want %q (the scratch root)", filepath.Dir(spoolPath), scratch)
	}
	if !strings.HasPrefix(filepath.Base(spoolPath), snapshotSpoolPrefix) {
		t.Fatalf("spool name = %q, want prefix %q", filepath.Base(spoolPath), snapshotSpoolPrefix)
	}
	if !strings.HasSuffix(spoolPath, ".tar") {
		t.Fatalf("spool name = %q, want .tar suffix", filepath.Base(spoolPath))
	}
	// The spool must round-trip the working set.
	f, err := os.Open(spoolPath)
	if err != nil {
		t.Fatalf("open spool: %v", err)
	}
	defer func() { _ = f.Close() }()
	files := map[string]string{}
	tr := tar.NewReader(f)
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
		t.Fatalf("spool tar = %v", files)
	}
	// Cleanup removes the spool.
	cleanup()
	if _, err := os.Stat(spoolPath); !os.IsNotExist(err) {
		t.Fatalf("spool not removed by cleanup: stat err = %v", err)
	}
}

// UploadSnapshot streams the spool file with the correct headers and returns
// the API's generation from the response (issue #1710).
func TestUploadSnapshotStreamsSpoolWithHeaders(t *testing.T) {
	// Create a spool from a real working set using PackSnapshot.
	scratch := t.TempDir()
	srcDir := filepath.Join(scratch, "server")
	if err := os.MkdirAll(srcDir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "server.properties"), []byte("p"), 0o640); err != nil {
		t.Fatal(err)
	}
	c := New(nil)
	spoolPath, cleanup, err := c.PackSnapshot(context.Background(), srcDir)
	if err != nil {
		t.Fatalf("PackSnapshot: %v", err)
	}
	defer cleanup()

	var gotLen int64
	var gotBaseGen string
	var gotWorkerID string
	var gotAuth string
	var received []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotLen = r.ContentLength
		gotBaseGen = r.Header.Get("X-Working-Set-Base-Generation")
		gotWorkerID = r.Header.Get("X-Worker-Id")
		gotAuth = r.Header.Get("Authorization")
		received, _ = io.ReadAll(r.Body)
		w.Header().Set("X-Working-Set-Generation", "42")
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	uploadClient := New(srv.Client())
	gen, err := uploadClient.UploadSnapshot(context.Background(), srv.URL, "tok", spoolPath, 7, "worker-7")
	if err != nil {
		t.Fatalf("UploadSnapshot: %v", err)
	}
	if gen != 42 {
		t.Fatalf("generation = %d, want 42", gen)
	}
	if gotAuth != "Bearer tok" {
		t.Fatalf("Authorization = %q, want %q", gotAuth, "Bearer tok")
	}
	if gotBaseGen != "7" {
		t.Fatalf("X-Working-Set-Base-Generation = %q, want %q", gotBaseGen, "7")
	}
	if gotWorkerID != "worker-7" {
		t.Fatalf("X-Worker-Id = %q, want %q", gotWorkerID, "worker-7")
	}
	if gotLen <= 0 || gotLen != int64(len(received)) {
		t.Fatalf("Content-Length = %d, body len = %d (must match and be > 0)", gotLen, len(received))
	}
	// The uploaded tar must contain the packed file.
	files := map[string]string{}
	tarR := tar.NewReader(bytes.NewReader(received))
	for {
		h, err := tarR.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if h.Typeflag == tar.TypeReg {
			b, _ := io.ReadAll(tarR)
			files[h.Name] = string(b)
		}
	}
	if files["server.properties"] != "p" {
		t.Fatalf("uploaded tar = %v", files)
	}
}

func TestSnapshotEmptyDirUploadsEmptyTar(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", filepath.Join(t.TempDir(), "absent"), 0, ""); err != nil {
		t.Fatalf("Snapshot of absent dir: %v", err)
	}
}

func TestSnapshotPropagatesServerError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
	}))
	defer srv.Close()

	c := New(srv.Client())
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", t.TempDir(), 0, ""); err == nil {
		t.Fatal("expected an error for a 400 response")
	}
}

// closedDataPlane starts an httptest server, takes its client and a data-plane URL
// under it, then shuts it down — so a transfer against that URL fails the way an
// unreachable data plane does (nothing listens: connection refused).
func closedDataPlane(t *testing.T) (*http.Client, string) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	client, transferURL := srv.Client(), srv.URL+"/api/data-plane/communities/c1/servers/s1/working-set"
	srv.Close()
	return client, transferURL
}

// TestHydrateErrorNamesTheDataPlaneURL pins that a hydrate against an unreachable
// data plane names the URL the Worker was handed (issue #2595). An operator who
// keeps the shipped compose default (MCD_API_SERVER__DATA_PLANE_BASE_URL pinned to
// the compose-internal http://api:8000) and then adds a Worker on a second host
// gets no boot-time signal at all — the variable is set, so the API's startup
// warning stays quiet (DEPLOYMENT.md Section 8). The first failed transfer is the
// only signal, so the URL has to be readable off it rather than inferred.
//
// The property comes from net/http wrapping transport failures in *url.Error, which
// formats the URL. That is incidental today; pinned here so a later rewrite of these
// error paths (a sanitized message, a sentinel error, a wrap that drops %w) cannot
// silently take the diagnosis away. Asserts on the URL only — the dial error text
// under it is resolver- and platform-dependent.
func TestHydrateErrorNamesTheDataPlaneURL(t *testing.T) {
	client, transferURL := closedDataPlane(t)

	_, err := New(client).Hydrate(context.Background(), transferURL, "tok", filepath.Join(t.TempDir(), "dest"))
	if err == nil {
		t.Fatal("expected an error hydrating from an unreachable data plane")
	}
	if !strings.Contains(err.Error(), transferURL) {
		t.Fatalf("hydrate error must name the data-plane URL %q, got: %v", transferURL, err)
	}
}

// TestSnapshotErrorNamesTheDataPlaneURL is TestHydrateErrorNamesTheDataPlaneURL for
// the push direction: the snapshot upload is the other half of the transfer pair the
// misconfiguration breaks, and it fails on its own error path.
func TestSnapshotErrorNamesTheDataPlaneURL(t *testing.T) {
	client, transferURL := closedDataPlane(t)

	_, err := New(client).Snapshot(context.Background(), transferURL, "tok", t.TempDir(), 0, "worker-1")
	if err == nil {
		t.Fatal("expected an error snapshotting to an unreachable data plane")
	}
	if !strings.Contains(err.Error(), transferURL) {
		t.Fatalf("snapshot error must name the data-plane URL %q, got: %v", transferURL, err)
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
	if _, err := c.Snapshot(context.Background(), srv.URL, "tok", srcDir, 0, ""); err != nil {
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
	err := unpackAndSwap(bytes.NewReader(body), dest, 0, slog.Default())
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

// A 200 hydrate must REPLACE destDir with a different directory object, not rewrite it
// in place. The instancemanager's generation-stamp guard (issue #2284) detects a
// concurrent re-placement by comparing the working dir's identity (os.SameFile against
// a pinned descriptor) across the snapshot's window — it is correct ONLY because this
// swap is a rename. Nothing else pins that, so an "optimisation" here that unpacked
// straight into destDir would silently defeat the guard and let a stale snapshot stamp
// its generation onto a tree it never packed.
func TestHydrateSwapChangesDestDirIdentity(t *testing.T) {
	body := tarOf(map[string]string{"server.properties": "new"})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	dest := filepath.Join(t.TempDir(), "server")
	if err := os.MkdirAll(dest, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "server.properties"), []byte("old"), 0o640); err != nil {
		t.Fatal(err)
	}
	before, err := os.Stat(dest)
	if err != nil {
		t.Fatal(err)
	}

	c := New(srv.Client())
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("Hydrate: %v", err)
	}

	after, err := os.Stat(dest)
	if err != nil {
		t.Fatal(err)
	}
	if os.SameFile(before, after) {
		t.Fatal("destDir is the SAME directory object after a 200 hydrate: the swap no longer " +
			"replaces by rename, so instancemanager's #2284 identity guard can no longer detect " +
			"a concurrent re-placement")
	}
}

// A SECOND hydrate over the same id must KEEP the tree already at .displaced-<id> and
// discard the working set it just displaced (oldest-wins, issue #2278). A surviving
// .displaced-<id> proves no snapshot for this id has succeeded since it was created
// (any success calls sweepDisplaced), so both trees are unpublished branches; the policy
// retains the FIRST one. At most one displaced tree per server still holds (#906), and
// the superseded set must leave no .hydrate-* leftover behind.
func TestHydrateKeepsOldestDisplacedTree(t *testing.T) {
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

	// Exactly one displaced tree, holding the FIRST-retained (v1) displaced content.
	got, err := os.ReadFile(filepath.Join(scratch, ".displaced-server", "gen"))
	if err != nil {
		t.Fatalf("displaced tree missing after second hydrate: %v", err)
	}
	if string(got) != "v1" {
		t.Fatalf("displaced content = %q, want %q (oldest-wins: the retained tree is never replaced, issue #2278)", got, "v1")
	}
	// No second .displaced-* sibling accumulated, and the superseded v2 set left no
	// .hydrate-* leftover pinning disk.
	entries, err := os.ReadDir(scratch)
	if err != nil {
		t.Fatal(err)
	}
	displacedCount := 0
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".displaced-") {
			displacedCount++
		}
		if strings.HasPrefix(e.Name(), ".hydrate-") {
			t.Fatalf("superseded set left a .hydrate-* leftover: %q", e.Name())
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
//
// SCOPE (issue #2278): this rule applies when the .displaced-<id> slot is EMPTY, as in
// this fixture. When it is already occupied, oldest-wins keeps the tree that is there
// and deliberately parks the superseded live set under a sweepable .hydrate-<id>-* name
// — see TestSwapParksSupersededSetUnderSweepableName. That is not a regression of #910:
// a recovery copy still sits at .displaced-<id> throughout.
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
	if err := unpackAndSwap(bytes.NewReader(body), dest, 0, slog.Default()); err == nil {
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
// displace, so it must leave that .displaced-<id> tree intact — nothing in the slot is
// touched unless a live destDir exists to displace.
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
	if err := unpackAndSwap(bytes.NewReader(body), dest, 0, slog.Default()); err == nil {
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

// The generation marker must be present in the temp tree BEFORE the swap-in rename
// (issue #917 bug 1): if it is written after the swap, a crash between swap-in and
// marker write leaves a destDir with no marker — the API reads gen 0 and re-dispatches
// hydrate, and that spurious retry discards this working set whenever a .displaced-<id>
// is retained (issue #2278).
func TestGenerationMarkerPresentAtSwapTime(t *testing.T) {
	const servedGen uint64 = 42
	body := tarOf(map[string]string{"server.properties": "new"})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("X-Working-Set-Generation", strconv.FormatUint(servedGen, 10))
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")

	// Inject a swapRename that inspects the temp tree for the marker at swap-in
	// time, then proceeds with the real rename.
	orig := swapRename
	var markerAtSwap string
	swapRename = func(src, dst string) error {
		data, err := os.ReadFile(filepath.Join(src, generationMarkerFile))
		if err != nil {
			markerAtSwap = ""
		} else {
			markerAtSwap = string(data)
		}
		return os.Rename(src, dst)
	}
	defer func() { swapRename = orig }()

	c := New(srv.Client())
	gen, err := c.Hydrate(context.Background(), srv.URL, "tok", dest)
	if err != nil {
		t.Fatalf("Hydrate: %v", err)
	}
	if gen != servedGen {
		t.Fatalf("generation = %d, want %d", gen, servedGen)
	}
	if markerAtSwap != strconv.FormatUint(servedGen, 10) {
		t.Fatalf("marker at swap-in time = %q, want %q (issue #917: marker must be written before swap)",
			markerAtSwap, strconv.FormatUint(servedGen, 10))
	}
}

// Prior displaced tree must survive a swap failure when both destDir and
// .displaced-<id> exist (issue #917 bug 2): the prior displaced must not be deleted
// before the swap-in succeeds, so a swap failure leaves both the old destDir and the
// prior displaced recoverable. Under oldest-wins (issue #2278) this also covers the
// restore path: the retained tree is never touched at all, and the superseded set is
// renamed back from its aside name instead of being dropped.
func TestPriorDisplacedSurvivesSwapFailure(t *testing.T) {
	orig := swapRename
	swapRename = func(_, _ string) error { return errors.New("forced swap failure") }
	defer func() { swapRename = orig }()

	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	if err := os.MkdirAll(dest, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "gen"), []byte("current"), 0o640); err != nil {
		t.Fatal(err)
	}
	// A prior displaced tree from a previous hydrate.
	displaced := filepath.Join(scratch, ".displaced-server")
	if err := os.MkdirAll(displaced, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(displaced, "gen"), []byte("prior-displaced"), 0o640); err != nil {
		t.Fatal(err)
	}

	body := tarOf(map[string]string{"server.properties": "new"})
	if err := unpackAndSwap(bytes.NewReader(body), dest, 99, slog.Default()); err == nil {
		t.Fatal("expected unpackAndSwap to fail when the swap rename fails")
	}

	// The prior displaced tree must survive (not be deleted before the swap succeeded).
	got, err := os.ReadFile(filepath.Join(displaced, "gen"))
	if err != nil {
		t.Fatalf("prior displaced tree lost on swap failure (issue #917): %v", err)
	}
	if string(got) != "prior-displaced" {
		t.Fatalf("prior displaced content = %q, want %q", got, "prior-displaced")
	}
	// The current working set must also be restored.
	got, err = os.ReadFile(filepath.Join(dest, "gen"))
	if err != nil {
		t.Fatalf("current working set not restored: %v", err)
	}
	if string(got) != "current" {
		t.Fatalf("destDir content = %q, want %q", got, "current")
	}
}

// Pins the exact on-disk layout AT THE INSTANT of the swap-in rename when the
// .displaced-<id> slot is already occupied (oldest-wins, issue #2278):
//
//	.displaced-server        → still holds "prior"   (retained, never renamed)
//	.hydrate-server-superseded-* → holds "current"   (the set this hydrate supersedes)
//	server                   → absent                (parked aside, not yet swapped in)
//
// This is the test that catches a regression to newest-wins: under that policy
// .displaced-server would hold "current" at this instant instead. The superseded set
// must sit under a .hydrate-<id>-* name so a crash in this window is reclaimable by
// every existing sweeper, and the live set must NOT have been deleted — a swap failure
// here still has to be able to put it back.
func TestSwapParksSupersededSetUnderSweepableName(t *testing.T) {
	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	if err := os.MkdirAll(dest, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "gen"), []byte("current"), 0o640); err != nil {
		t.Fatal(err)
	}
	displaced := filepath.Join(scratch, ".displaced-server")
	if err := os.MkdirAll(displaced, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(displaced, "gen"), []byte("prior"), 0o640); err != nil {
		t.Fatal(err)
	}

	orig := swapRename
	var retainedAtSwap, supersededAtSwap string
	var destPresentAtSwap bool
	swapRename = func(src, dst string) error {
		if _, err := os.Stat(dest); err == nil {
			destPresentAtSwap = true
		}
		entries, _ := os.ReadDir(scratch)
		for _, e := range entries {
			d, rerr := os.ReadFile(filepath.Join(scratch, e.Name(), "gen"))
			if rerr != nil {
				continue
			}
			switch string(d) {
			case "prior":
				retainedAtSwap = e.Name()
			case "current":
				supersededAtSwap = e.Name()
			}
		}
		return os.Rename(src, dst)
	}
	defer func() { swapRename = orig }()

	body := tarOf(map[string]string{"server.properties": "new"})
	if err := unpackAndSwap(bytes.NewReader(body), dest, 99, slog.Default()); err != nil {
		t.Fatalf("unpackAndSwap: %v", err)
	}

	if destPresentAtSwap {
		t.Fatal("destDir still present at swap-in time; the live set was not parked aside first")
	}
	if retainedAtSwap != ".displaced-server" {
		t.Fatalf("retained tree at swap-in = %q, want %q (oldest-wins keeps the existing tree in place, issue #2278)",
			retainedAtSwap, ".displaced-server")
	}
	if !strings.HasPrefix(supersededAtSwap, ".hydrate-server-superseded-") {
		t.Fatalf("superseded set at swap-in = %q, want a .hydrate-server-superseded-* name (sweepable, issue #2278)",
			supersededAtSwap)
	}
}

// After a SUCCESSFUL swap with a prior displaced tree present, the retained tree keeps
// its content, the superseded set is dropped (no .hydrate-* leftover pinning disk), and
// destDir holds the new set plus its generation marker (issue #2278).
func TestSupersededSetDroppedOnSuccessfulSwap(t *testing.T) {
	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	if err := os.MkdirAll(dest, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "gen"), []byte("current"), 0o640); err != nil {
		t.Fatal(err)
	}
	displaced := filepath.Join(scratch, ".displaced-server")
	if err := os.MkdirAll(displaced, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(displaced, "gen"), []byte("prior"), 0o640); err != nil {
		t.Fatal(err)
	}

	body := tarOf(map[string]string{"server.properties": "new"})
	if err := unpackAndSwap(bytes.NewReader(body), dest, 99, slog.Default()); err != nil {
		t.Fatalf("unpackAndSwap: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(displaced, "gen"))
	if err != nil || string(got) != "prior" {
		t.Fatalf("retained displaced content = %q, %v (want %q)", got, err, "prior")
	}
	if got, err = os.ReadFile(filepath.Join(dest, "server.properties")); err != nil || string(got) != "new" {
		t.Fatalf("destDir server.properties = %q, %v (want %q)", got, err, "new")
	}
	if got, err = os.ReadFile(filepath.Join(dest, generationMarkerFile)); err != nil || string(got) != "99" {
		t.Fatalf("destDir generation marker = %q, %v (want %q)", got, err, "99")
	}
	entries, err := os.ReadDir(scratch)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".hydrate-") {
			t.Fatalf("superseded set left a .hydrate-* leftover after a successful swap: %q", e.Name())
		}
	}
}

// A failed swap-in must put the superseded set back at destDir and leave the retained
// tree untouched (issue #2278): oldest-wins drops the superseded set only AFTER the
// swap-in succeeds, so a failure loses nothing and leaves no scratch leftovers.
func TestSupersededSetRestoredWhenSwapFails(t *testing.T) {
	orig := swapRename
	swapRename = func(_, _ string) error { return errors.New("forced swap failure") }
	defer func() { swapRename = orig }()

	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	if err := os.MkdirAll(dest, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "gen"), []byte("current"), 0o640); err != nil {
		t.Fatal(err)
	}
	displaced := filepath.Join(scratch, ".displaced-server")
	if err := os.MkdirAll(displaced, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(displaced, "gen"), []byte("prior"), 0o640); err != nil {
		t.Fatal(err)
	}

	body := tarOf(map[string]string{"server.properties": "new"})
	if err := unpackAndSwap(bytes.NewReader(body), dest, 99, slog.Default()); err == nil {
		t.Fatal("expected unpackAndSwap to fail when the swap rename fails")
	}

	got, err := os.ReadFile(filepath.Join(dest, "gen"))
	if err != nil || string(got) != "current" {
		t.Fatalf("superseded set not restored to destDir = %q, %v (want %q)", got, err, "current")
	}
	if got, err = os.ReadFile(filepath.Join(displaced, "gen")); err != nil || string(got) != "prior" {
		t.Fatalf("retained displaced content = %q, %v (want %q)", got, err, "prior")
	}
	entries, err := os.ReadDir(scratch)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Name() != "server" && e.Name() != ".displaced-server" {
			t.Fatalf("leftover entry in scratch root after a failed swap: %q", e.Name())
		}
	}
}

// A crash between the aside-rename and the swap-in must CONVERGE on the next hydrate,
// not accumulate (issue #2278, the S2 crash row). The post-crash fixture is destDir
// absent, the retained tree at .displaced-<id>, the superseded set and the unpacked temp
// tree both under .hydrate-<id>-* names. A fresh hydrate sweeps both leftovers, finds
// nothing to displace, and swaps in — ending at the same state a clean run produces,
// with the oldest tree still retained.
func TestCrashBetweenAsideAndSwapConvergesToOldest(t *testing.T) {
	body := tarOf(map[string]string{"server.properties": "fresh"})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	mkTree := func(dir, content string) {
		if err := os.MkdirAll(dir, 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "gen"), []byte(content), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	displaced := filepath.Join(scratch, ".displaced-server")
	mkTree(displaced, "prior")
	mkTree(filepath.Join(scratch, ".hydrate-server-superseded-123"), "current")
	mkTree(filepath.Join(scratch, ".hydrate-server-456"), "half-unpacked")

	c := New(srv.Client())
	if _, err := c.Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("Hydrate: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(dest, "server.properties"))
	if err != nil || string(got) != "fresh" {
		t.Fatalf("destDir server.properties = %q, %v (want %q)", got, err, "fresh")
	}
	if got, err = os.ReadFile(filepath.Join(displaced, "gen")); err != nil || string(got) != "prior" {
		t.Fatalf("retained displaced content = %q, %v (want %q — the crash window must converge, not lose it)", got, err, "prior")
	}
	entries, err := os.ReadDir(scratch)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".hydrate-") {
			t.Fatalf("crash leftover not reclaimed by the next hydrate: %q", e.Name())
		}
	}
}

// Junk in the .displaced-<id> slot must NOT shadow a real world (issue #2278). Under
// newest-wins junk was simply overwritten; under oldest-wins an occupied-looking slot
// makes the hydrate discard the live set, so "occupied" must mean "a directory holding
// something", not merely "a name exists". Realistic sources are a partially-failed
// best-effort RemoveAll in sweepDisplaced and an operator's half-finished manual
// cleanup (STORAGE.md Section 4.6). In both sub-cases the hydrate must take the
// ordinary path: the live set lands at .displaced-<id> and is recoverable.
func TestJunkDisplacedSlotDoesNotShadowLiveSet(t *testing.T) {
	cases := []struct {
		name string
		junk func(t *testing.T, path string)
	}{
		{"empty dir", func(t *testing.T, path string) {
			if err := os.MkdirAll(path, 0o750); err != nil {
				t.Fatal(err)
			}
		}},
		{"regular file", func(t *testing.T, path string) {
			if err := os.WriteFile(path, []byte("leftover"), 0o640); err != nil {
				t.Fatal(err)
			}
		}},
		// The routine world-less shape, not an exotic one: a 204 hydrate returns without
		// creating destDir, and the caller's writeGeneration then makes <scratch>/<id>
		// holding ONLY the marker. hasWorkingSet reports that as not-held, so the next 200
		// hydrate parks it at .displaced-<id> by the ordinary path — and it must not then
		// shadow a real world.
		{"marker only", func(t *testing.T, path string) {
			if err := os.MkdirAll(path, 0o750); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(path, generationMarkerFile), []byte("7"), 0o640); err != nil {
				t.Fatal(err)
			}
		}},
		// Same, via a marker TEMP sibling: writeGeneration writes the marker atomically
		// through a ".mcsd_generation-XXXX" temp + rename, so a crash before the rename
		// leaves one behind. The match must be by prefix, as hasWorkingSet does (#2279).
		{"marker temp sibling only", func(t *testing.T, path string) {
			if err := os.MkdirAll(path, 0o750); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(path, generationMarkerFile+"-abc123"), []byte("7"), 0o640); err != nil {
				t.Fatal(err)
			}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			scratch := t.TempDir()
			dest := filepath.Join(scratch, "server")
			if err := os.MkdirAll(dest, 0o750); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(dest, "gen"), []byte("live"), 0o640); err != nil {
				t.Fatal(err)
			}
			displaced := filepath.Join(scratch, ".displaced-server")
			tc.junk(t, displaced)

			body := tarOf(map[string]string{"server.properties": "new"})
			if err := unpackAndSwap(bytes.NewReader(body), dest, 99, slog.Default()); err != nil {
				t.Fatalf("unpackAndSwap: %v", err)
			}

			got, err := os.ReadFile(filepath.Join(displaced, "gen"))
			if err != nil {
				t.Fatalf("live working set not displaced to .displaced-server; junk in the slot shadowed a real world (issue #2278): %v", err)
			}
			if string(got) != "live" {
				t.Fatalf("displaced content = %q, want %q", got, "live")
			}
			if got, err = os.ReadFile(filepath.Join(dest, "server.properties")); err != nil || string(got) != "new" {
				t.Fatalf("destDir server.properties = %q, %v (want %q)", got, err, "new")
			}
		})
	}
}

// An unexpected error reading the .displaced-<id> slot must FAIL the hydrate, never be
// reclassified as a decision (issue #2278). The slot check answers "which world do we
// keep", so a transient EACCES/EMFILE must not silently become "the slot is occupied,
// discard the live working set" — nor "the slot is junk, delete it". Failing loses
// nothing: the caller maps a hydrate error to CommandErrorTransferFailed exactly as it
// does for ENOSPC, and the transfer is retried (STORAGE.md Section 4.6).
//
// The failure is injected through the readDir seam rather than a chmod fixture: a
// mode-000 directory is readable by root, so a chmod-based test silently stops asserting
// anything whenever the suite runs as root.
func TestUnreadableDisplacedSlotFailsHydrateWithoutDiscarding(t *testing.T) {
	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	if err := os.MkdirAll(filepath.Join(dest, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "world", "r.0.0.mca"), []byte("live-world"), 0o640); err != nil {
		t.Fatal(err)
	}
	displaced := filepath.Join(scratch, ".displaced-server")
	if err := os.MkdirAll(filepath.Join(displaced, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(displaced, "world", "r.0.0.mca"), []byte("retained-world"), 0o640); err != nil {
		t.Fatal(err)
	}

	orig := readDir
	readDir = func(dir string) ([]os.DirEntry, error) {
		if dir == displaced {
			return nil, os.ErrPermission
		}
		return os.ReadDir(dir)
	}
	defer func() { readDir = orig }()

	body := tarOf(map[string]string{"server.properties": "new"})
	err := unpackAndSwap(bytes.NewReader(body), dest, 99, slog.Default())
	if err == nil {
		t.Fatal("expected unpackAndSwap to fail when the displaced slot cannot be read")
	}
	if !errors.Is(err, os.ErrPermission) {
		t.Fatalf("error = %v, want a permission error propagated to the caller", err)
	}

	// The live working set is untouched — not displaced, not discarded.
	got, readErr := os.ReadFile(filepath.Join(dest, "world", "r.0.0.mca"))
	if readErr != nil || string(got) != "live-world" {
		t.Fatalf("live working set = %q, %v (want %q untouched at destDir)", got, readErr, "live-world")
	}
	// The slot is byte-identical: neither cleared as junk nor overwritten.
	if got, readErr = os.ReadFile(filepath.Join(displaced, "world", "r.0.0.mca")); readErr != nil || string(got) != "retained-world" {
		t.Fatalf("displaced slot = %q, %v (want %q untouched)", got, readErr, "retained-world")
	}
	// No leftovers pinning disk.
	entries, readErr := os.ReadDir(scratch)
	if readErr != nil {
		t.Fatal(readErr)
	}
	for _, e := range entries {
		if e.Name() != "server" && e.Name() != ".displaced-server" {
			t.Fatalf("leftover entry in scratch root after a failed slot read: %q", e.Name())
		}
	}
}

// Discarding a working set must be visible to an operator (issue #2278): oldest-wins
// gives up the newer unpublished branch, and the WARN naming BOTH paths is the whole
// mitigation for that. Without it the loss is silent.
func TestDiscardWarnsWithBothPaths(t *testing.T) {
	scratch := t.TempDir()
	dest := filepath.Join(scratch, "server")
	if err := os.MkdirAll(dest, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "gen"), []byte("current"), 0o640); err != nil {
		t.Fatal(err)
	}
	displaced := filepath.Join(scratch, ".displaced-server")
	if err := os.MkdirAll(displaced, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(displaced, "gen"), []byte("prior"), 0o640); err != nil {
		t.Fatal(err)
	}

	h := &capturingHandler{}
	body := tarOf(map[string]string{"server.properties": "new"})
	if err := unpackAndSwap(bytes.NewReader(body), dest, 99, slog.New(h)); err != nil {
		t.Fatalf("unpackAndSwap: %v", err)
	}

	var rec *slog.Record
	for i := range h.records {
		if h.records[i].Level == slog.LevelWarn && strings.Contains(h.records[i].Message, "discarding") {
			rec = &h.records[i]
			break
		}
	}
	if rec == nil {
		t.Fatalf("no discard WARN emitted; the oldest-wins data loss would be silent (issue #2278). records: %v", h.records)
	}
	attrs := map[string]string{}
	rec.Attrs(func(a slog.Attr) bool {
		attrs[a.Key] = a.Value.String()
		return true
	})
	if attrs["server_id"] != "server" {
		t.Fatalf("WARN server_id = %q, want %q", attrs["server_id"], "server")
	}
	if attrs["retained"] != displaced {
		t.Fatalf("WARN retained = %q, want %q", attrs["retained"], displaced)
	}
	if attrs["discarded"] != dest {
		t.Fatalf("WARN discarded = %q, want %q", attrs["discarded"], dest)
	}
}
