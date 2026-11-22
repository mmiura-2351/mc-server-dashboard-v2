package instancemanager

import (
	"archive/tar"
	"bytes"
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/datatransfer"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/regionfsck"
)

// These tests pin the generation marker's authentication (issue #3349): the marker
// lies in a directory the server's own code can write, so the Worker believes it only
// when it carries an HMAC over (server id, generation) under the Worker's key, which
// lies at the scratch root, outside every working set.

// plantMarker replaces dir's marker with data the way the server would from inside
// its container: it owns the directory, so it unlinks the Worker's file and creates
// its own.
func plantMarker(t *testing.T, dir string, data []byte) {
	t.Helper()
	marker := filepath.Join(dir, generationFile)
	if err := os.Remove(marker); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	if err := os.WriteFile(marker, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

// heldGenerationOf returns the generation HeldServers advertises for serverID, and
// whether it advertises the server at all.
func heldGenerationOf(m *Manager, serverID string) (uint64, bool) {
	for _, h := range m.HeldServers() {
		if h.ServerID == serverID {
			return h.Generation, true
		}
	}
	return 0, false
}

// A marker the Worker wrote for one server, copied into another server's working
// set, reads as generation 0 there: the MAC covers the server id.
func TestAMarkerCopiedFromAnotherServerIsNotBelieved(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), hydrateCmd()); !res.Success {
		t.Fatalf("hydrate = %+v, want success", res)
	}
	if gen, ok := heldGenerationOf(m, s1); !ok || gen != 12 {
		t.Fatalf("s1 held at %d (advertised %v), want its genuine generation 12", gen, ok)
	}
	genuine, err := os.ReadFile(filepath.Join(m.scratchDir, s1, generationFile))
	if err != nil {
		t.Fatal(err)
	}

	plantMarker(t, seedScratch(t, m, s2), genuine)

	if gen, ok := heldGenerationOf(m, s2); !ok || gen != 0 {
		t.Fatalf("s2 held at %d (advertised %v), want 0: s1's marker must not verify for s2", gen, ok)
	}
	for _, h := range ScanHeldServers(m.scratchDir, m.genKey, regionfsck.Checker{}, true, nil) {
		if h.ServerID == s2 && h.Generation != 0 {
			t.Fatalf("boot scan held s2 at %d, want 0: s1's marker must not verify for s2", h.Generation)
		}
	}
}

// A marker whose generation is edited keeps a MAC that no longer matches it, so it
// reads as generation 0.
func TestAMarkerWithAnEditedGenerationIsNotBelieved(t *testing.T) {
	tr := &fakeTransfer{gen: 4242}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	dir := seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), hydrateCmd()); !res.Success {
		t.Fatalf("hydrate = %+v, want success", res)
	}
	genuine, err := os.ReadFile(filepath.Join(dir, generationFile))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(genuine, []byte("4242")) {
		t.Fatalf("marker %q does not name its generation 4242", genuine)
	}

	plantMarker(t, dir, bytes.Replace(genuine, []byte("4242"), []byte("9999"), 1))

	if gen, ok := heldGenerationOf(m, s1); !ok || gen != 0 {
		t.Fatalf("s1 held at %d (advertised %v), want 0 for a marker whose generation was edited", gen, ok)
	}
}

// A marker that fails verification is logged once per server, not on every
// registration: each one reads the marker again.
func TestAMarkerThatFailsVerificationIsWarnedOncePerServer(t *testing.T) {
	h := &syncSlogHandler{}
	m := newManager(t, &fakeDriver{}, nil).WithLogger(slog.New(h))
	// A key of its own: testGenerationKey remembers what other tests warned about.
	m.WithGenerationKey(LoadGenerationKey(m.scratchDir, nil))
	plantMarker(t, seedScratch(t, m, s1), []byte("999"))
	plantMarker(t, seedScratch(t, m, s2), []byte("not-a-number"))

	for range 3 {
		m.HeldServers()
	}

	warned := map[string]int{}
	for _, r := range h.snapshot() {
		if r.Level != slog.LevelWarn || !strings.Contains(r.Message, "generation marker failed verification") {
			continue
		}
		r.Attrs(func(a slog.Attr) bool {
			if a.Key == "server_id" {
				warned[a.Value.String()]++
			}
			return true
		})
	}
	if warned[s1] != 1 || warned[s2] != 1 || len(warned) != 2 {
		t.Fatalf("verification WARNs per server = %v, want exactly one each for %s and %s", warned, s1, s2)
	}
}

// generationServer serves a hydrate of a one-file working set at hydrateGen and
// answers a snapshot publish with snapshotGen, as the API's data plane does.
func generationServer(t *testing.T, hydrateGen, snapshotGen string) *httptest.Server {
	t.Helper()
	var body bytes.Buffer
	tw := tar.NewWriter(&body)
	content := []byte("world")
	if err := tw.WriteHeader(&tar.Header{Name: "level.dat", Mode: 0o640, Size: int64(len(content))}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(content); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			w.Header().Set("X-Working-Set-Generation", hydrateGen)
			_, _ = w.Write(body.Bytes())
			return
		}
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("X-Working-Set-Generation", snapshotGen)
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(srv.Close)
	return srv
}

// observingTransfer runs afterHydrate once the wrapped Transfer's Hydrate returns,
// before the manager's own marker write: what it sees is the marker the hydrate's 200
// path wrote into the unpacked tree before swapping it in.
type observingTransfer struct {
	Transfer
	afterHydrate func(workingDir string)
}

func (o observingTransfer) Hydrate(ctx context.Context, url, token, workingDir string, marker func(uint64) []byte) (uint64, error) {
	gen, err := o.Transfer.Hydrate(ctx, url, token, workingDir, marker)
	o.afterHydrate(workingDir)
	return gen, err
}

// A marker the Worker writes is believed by every reader: the hydrate's 200 path
// writes it into the unpacked tree (datatransfer's unpackAndSwap), the running
// snapshot stamps the generation it published, and the held-set scans read both back.
// On disk it is the versioned, signed form, not a bare number.
func TestAGenuineMarkerRoundTripsThroughHydrateSnapshotAndScan(t *testing.T) {
	srv := generationServer(t, "42", "43")
	m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"})
	var unpacked uint64
	var unpackedErr error
	m.WithTransfer(observingTransfer{Transfer: datatransfer.New(srv.Client()), afterHydrate: func(dir string) {
		unpacked, unpackedErr = readGenerationMarker(m.genKey, dir, s1)
	}})
	dir := filepath.Join(m.scratchDir, s1)
	hydrate, start, snapshot := hydrateCmd(), startCmd(), snapshotCmd()
	hydrate.TransferURL, snapshot.TransferURL = srv.URL, srv.URL
	boot := func() uint64 {
		t.Helper()
		held := ScanHeldServers(m.scratchDir, m.genKey, regionfsck.Checker{}, true, nil)
		if len(held) != 1 || held[0].ServerID != s1 {
			t.Fatalf("boot scan = %+v, want s1 alone", held)
		}
		return held[0].Generation
	}

	if res := m.Handle(context.Background(), hydrate); !res.Success || res.HeldGeneration == nil || *res.HeldGeneration != 42 {
		t.Fatalf("hydrate = %+v, want success declaring 42", res)
	}
	if unpacked != 42 || unpackedErr != nil {
		t.Fatalf("marker the hydrate unpacked = %d, %v; want 42 verified", unpacked, unpackedErr)
	}
	assertSignedMarker(t, dir, "42")
	if gen, ok := heldGenerationOf(m, s1); !ok || gen != 42 {
		t.Fatalf("held after hydrate = %d (advertised %v), want 42", gen, ok)
	}
	if gen := boot(); gen != 42 {
		t.Fatalf("boot scan after hydrate = %d, want 42", gen)
	}

	if res := m.Handle(context.Background(), start); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	if res := m.Handle(context.Background(), snapshot); !res.Success || res.HeldGeneration == nil || *res.HeldGeneration != 43 {
		t.Fatalf("snapshot = %+v, want success declaring 43", res)
	}
	assertSignedMarker(t, dir, "43")
	if gen, ok := heldGenerationOf(m, s1); !ok || gen != 43 {
		t.Fatalf("held after the snapshot stamp = %d (advertised %v), want 43", gen, ok)
	}
	if gen := boot(); gen != 43 {
		t.Fatalf("boot scan after the snapshot stamp = %d, want 43", gen)
	}
}

// assertSignedMarker fails unless dir's marker is the versioned, signed form naming
// gen.
func assertSignedMarker(t *testing.T, dir, gen string) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, generationFile))
	if err != nil {
		t.Fatal(err)
	}
	if fields := strings.Fields(string(data)); len(fields) != 3 || fields[0] != "v2" || fields[1] != gen || len(fields[2]) != 64 {
		t.Fatalf("marker = %q, want \"v2 %s <64 hex digits>\"", data, gen)
	}
}

// The key lies at the scratch root, outside every working set, so no container can
// read it: a container is given its own working set only.
func TestTheGenerationKeyLiesOutsideEveryWorkingSet(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{gen: 3})
	m.WithGenerationKey(LoadGenerationKey(m.scratchDir, nil))
	dir := seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), hydrateCmd()); !res.Success {
		t.Fatalf("hydrate = %+v, want success", res)
	}

	info, err := os.Lstat(filepath.Join(m.scratchDir, ".generation-key"))
	if err != nil {
		t.Fatalf("no generation key at the scratch root: %v", err)
	}
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 || info.Size() != 32 {
		t.Fatalf("generation key = %v, %d bytes; want a 0600 regular file of 32 bytes", info.Mode(), info.Size())
	}
	err = filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if strings.Contains(d.Name(), "generation-key") {
			t.Errorf("%s lies inside the working set", path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

// The MAC is compared in constant time (hmac.Equal), so how long a refusal takes
// tells a forger nothing about how much of its MAC was right. A timing difference
// cannot be tested for, so the call is pinned instead.
func TestTheMarkerMACIsComparedInConstantTime(t *testing.T) {
	file, err := parser.ParseFile(token.NewFileSet(), "generationkey.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	calls := map[string]bool{}
	ast.Inspect(file, func(n ast.Node) bool {
		if call, ok := n.(*ast.CallExpr); ok {
			if sel, ok := call.Fun.(*ast.SelectorExpr); ok {
				if pkg, ok := sel.X.(*ast.Ident); ok {
					calls[pkg.Name+"."+sel.Sel.Name] = true
				}
			}
		}
		return true
	})
	if !calls["hmac.Equal"] {
		t.Error("generationkey.go does not compare the MAC with hmac.Equal")
	}
	for _, timed := range []string{"bytes.Equal", "slices.Equal", "reflect.DeepEqual", "bytes.Compare", "strings.EqualFold"} {
		if calls[timed] {
			t.Errorf("generationkey.go calls %s, which returns at the first differing byte", timed)
		}
	}
}

// describeKey records what sits at the key's path — its type, and its content or link
// target — so a test can tell whether it was replaced.
func describeKey(t *testing.T, path string) string {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	switch {
	case info.Mode()&fs.ModeSymlink != 0:
		target, err := os.Readlink(path)
		if err != nil {
			t.Fatal(err)
		}
		return "symlink to " + target
	case info.IsDir():
		return "directory"
	default:
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		return "file " + string(data)
	}
}

// A key that exists but cannot be used is logged at ERROR and left as it is. Nothing
// is believed under it, so every held set reads as generation 0, and it is never
// replaced by a fresh key, which would leave the operator nothing to inspect. A start
// still succeeds: the marker is still written, so the launch guard finds it, but the
// hydrate declares no generation, since a marker the Worker cannot authenticate backs
// none.
func TestACorruptGenerationKeyIsNeitherBelievedNorReplaced(t *testing.T) {
	for _, tc := range []struct {
		name  string
		plant func(t *testing.T, path string)
	}{
		{"too short", func(t *testing.T, path string) {
			if err := os.WriteFile(path, []byte("short"), 0o600); err != nil {
				t.Fatal(err)
			}
		}},
		{"too long", func(t *testing.T, path string) {
			if err := os.WriteFile(path, bytes.Repeat([]byte{1}, 33), 0o600); err != nil {
				t.Fatal(err)
			}
		}},
		{"a symlink", func(t *testing.T, path string) {
			target := filepath.Join(t.TempDir(), "key")
			if err := os.WriteFile(target, bytes.Repeat([]byte{1}, 32), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(target, path); err != nil {
				t.Fatal(err)
			}
		}},
		{"a directory", func(t *testing.T, path string) {
			if err := os.Mkdir(path, 0o700); err != nil {
				t.Fatal(err)
			}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := &syncSlogHandler{}
			m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{gen: 7}).WithLogger(slog.New(h))
			keyPath := filepath.Join(m.scratchDir, ".generation-key")
			tc.plant(t, keyPath)
			before := describeKey(t, keyPath)

			key := LoadGenerationKey(m.scratchDir, m.logger)
			m.WithGenerationKey(key)

			errors := 0
			for _, r := range h.snapshot() {
				if r.Level == slog.LevelError && strings.Contains(r.Message, "generation key") {
					errors++
				}
			}
			if errors != 1 {
				t.Fatalf("ERROR records about the key = %d, want 1", errors)
			}

			dir := seedScratch(t, m, s1)
			if err := os.Remove(filepath.Join(dir, generationFile)); err != nil {
				t.Fatal(err)
			}
			res := m.Handle(context.Background(), hydrateCmd())
			if !res.Success || res.HeldGeneration != nil {
				t.Fatalf("hydrate = %+v, want success declaring no generation", res)
			}
			if marker, err := os.ReadFile(filepath.Join(dir, generationFile)); err != nil || string(marker) != "7" {
				t.Fatalf("marker after the hydrate = %q, %v; want the unsigned 7 it wrote", marker, err)
			}
			if res := m.Handle(context.Background(), startCmd()); !res.Success {
				t.Fatalf("start = %+v, want success: the launch guard needs only the marker", res)
			}
			if gen, ok := heldGenerationOf(m, s1); !ok || gen != 0 {
				t.Fatalf("s1 held at %d (advertised %v), want 0 under an unusable key", gen, ok)
			}
			if held := ScanHeldServers(m.scratchDir, key, regionfsck.Checker{}, true, nil); len(held) != 1 || held[0].Generation != 0 {
				t.Fatalf("boot scan = %+v, want s1 at 0 under an unusable key", held)
			}
			if after := describeKey(t, keyPath); after != before {
				t.Fatalf("the key was replaced: %q, then %q", before, after)
			}
		})
	}
}

// A scratch root reached through a symlink is the operator's own path, so the key is
// loaded through it as through any other: only the key's own name is not followed.
func TestTheGenerationKeyLoadsThroughASymlinkedScratchRoot(t *testing.T) {
	target := filepath.Join(t.TempDir(), "target")
	if err := os.Mkdir(target, 0o750); err != nil {
		t.Fatal(err)
	}
	scratch := filepath.Join(t.TempDir(), "scratch")
	if err := os.Symlink(target, scratch); err != nil {
		t.Fatal(err)
	}
	h := &syncSlogHandler{}
	key := LoadGenerationKey(scratch, slog.New(h))
	if !key.usable() {
		t.Fatalf("key loaded through a symlinked scratch root is unusable; logged %+v", h.snapshot())
	}
	if info, err := os.Lstat(filepath.Join(target, ".generation-key")); err != nil || !info.Mode().IsRegular() {
		t.Fatalf("key in the scratch root's target = %v, %v; want a regular file", info, err)
	}
}

// An absent key is created on first use, and a second load reads the same key back,
// so markers written before a restart verify after it.
func TestTheGenerationKeyIsCreatedOnceAndReadBack(t *testing.T) {
	scratch := filepath.Join(t.TempDir(), "scratch")
	first := LoadGenerationKey(scratch, nil)
	dir := filepath.Join(scratch, s1)
	if err := writeGeneration(first, dir, s1, 5); err != nil {
		t.Fatal(err)
	}
	if gen, err := readGenerationMarker(LoadGenerationKey(scratch, nil), dir, s1); gen != 5 || err != nil {
		t.Fatalf("marker under the reloaded key = %d, %v; want 5", gen, err)
	}
}
