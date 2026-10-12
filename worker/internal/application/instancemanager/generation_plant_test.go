package instancemanager

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// These tests pin every touch of the generation marker against what a server can
// plant at it (issue #3336): the marker sits in the root of a working set the
// server's own code owns and writes (PR #3296), and the Worker touching it is root
// in the shipped topology. They run on a real filesystem, as whatever user runs the
// suite, so "outside the working set" is a sibling temp dir rather than a root-only
// file.

// outsideGeneration is the content of a file outside the working set that a planted
// link points at. It is a well-formed generation, so a reader that follows the link
// reports it.
const outsideGeneration = "42"

// markerPlant is one thing a server can put where the marker belongs.
type markerPlant struct {
	name string
	// plant puts it at path and returns the outside file it points at, or "".
	plant func(t *testing.T, path string) string
}

func writeOutsideGeneration(t *testing.T) string {
	t.Helper()
	outside := filepath.Join(t.TempDir(), "outside-generation")
	if err := os.WriteFile(outside, []byte(outsideGeneration), 0o600); err != nil {
		t.Fatal(err)
	}
	return outside
}

func markerPlants() []markerPlant {
	symlinkTo := func(target func(t *testing.T) string) func(t *testing.T, path string) string {
		return func(t *testing.T, path string) string {
			t.Helper()
			to := target(t)
			if err := os.Symlink(to, path); err != nil {
				t.Fatal(err)
			}
			return to
		}
	}
	return []markerPlant{
		{"symlink to an outside generation", symlinkTo(writeOutsideGeneration)},
		{"dangling symlink", func(t *testing.T, path string) string {
			t.Helper()
			if err := os.Symlink(filepath.Join(t.TempDir(), "nothing-here"), path); err != nil {
				t.Fatal(err)
			}
			return ""
		}},
		{"symlink to an endless device", func(t *testing.T, path string) string {
			t.Helper()
			if err := os.Symlink("/dev/zero", path); err != nil {
				t.Fatal(err)
			}
			return ""
		}},
		{"fifo without a writer", func(t *testing.T, path string) string {
			t.Helper()
			if err := unix.Mkfifo(path, 0o600); err != nil {
				t.Fatal(err)
			}
			return ""
		}},
		{"directory", func(t *testing.T, path string) string {
			t.Helper()
			if err := os.Mkdir(path, 0o700); err != nil {
				t.Fatal(err)
			}
			return ""
		}},
		{"sparse and huge", func(t *testing.T, path string) string {
			t.Helper()
			if err := os.WriteFile(path, nil, 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Truncate(path, 1<<40); err != nil {
				t.Skipf("the filesystem cannot hold a sparse 1 TiB file: %v", err)
			}
			return ""
		}},
		// Zero-padded past the cap: it parses as 42 when read whole, so only the size
		// bound refuses it.
		{"well-formed but over the size cap", func(t *testing.T, path string) string {
			t.Helper()
			padded := strings.Repeat("0", maxGenerationMarkerBytes) + outsideGeneration
			if err := os.WriteFile(path, []byte(padded), 0o600); err != nil {
				t.Fatal(err)
			}
			return ""
		}},
		{"not a number", func(t *testing.T, path string) string {
			t.Helper()
			if err := os.WriteFile(path, []byte("not-a-number"), 0o600); err != nil {
				t.Fatal(err)
			}
			return ""
		}},
	}
}

// within fails the test when fn has not returned in time: a marker touch that
// blocks (a FIFO without a writer) must fail the test, not hang the suite.
func within(t *testing.T, what string, fn func()) {
	t.Helper()
	done := make(chan struct{})
	go func() {
		defer close(done)
		fn()
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatalf("%s blocked", what)
	}
}

func assertUntouched(t *testing.T, outside string) {
	t.Helper()
	if outside == "" {
		return
	}
	got, err := os.ReadFile(outside)
	if err != nil || string(got) != outsideGeneration {
		t.Fatalf("the file outside the working set now reads %q (%v), want it untouched", got, err)
	}
}

func warned(h *capturingSlogHandler, fragment, serverID string) bool {
	for _, r := range h.records {
		if r.Level != slog.LevelWarn || !strings.Contains(r.Message, fragment) {
			continue
		}
		found := false
		r.Attrs(func(a slog.Attr) bool {
			found = a.Key == "server_id" && a.Value.String() == serverID
			return !found
		})
		if found {
			return true
		}
	}
	return false
}

func TestReadGenerationMarkerRefusesAPlantedMarker(t *testing.T) {
	for _, plant := range markerPlants() {
		t.Run(plant.name, func(t *testing.T) {
			dir := t.TempDir()
			outside := plant.plant(t, filepath.Join(dir, generationFile))

			var gen uint64
			var err error
			within(t, "the marker read", func() { gen, err = readGenerationMarker(dir) })

			if gen != 0 || err == nil {
				t.Fatalf("readGenerationMarker = %d, %v; want 0 and a refusal", gen, err)
			}
			assertUntouched(t, outside)
		})
	}
}

// An absent marker, or an absent working dir, is not a refusal: it is the ordinary
// state of a set that was never stamped, and reads as generation 0 without a WARN.
func TestReadGenerationMarkerReportsAnAbsentMarkerAsZeroWithoutAnError(t *testing.T) {
	for name, dir := range map[string]string{
		"marker absent":      t.TempDir(),
		"working dir absent": filepath.Join(t.TempDir(), "absent"),
	} {
		if gen, err := readGenerationMarker(dir); gen != 0 || err != nil {
			t.Errorf("%s: readGenerationMarker = %d, %v; want 0, nil", name, gen, err)
		}
	}
}

// The largest generation there is, with the trailing newline an operator's echo
// adds, fits the cap.
func TestReadGenerationMarkerAcceptsTheLargestGeneration(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, generationFile), []byte("18446744073709551615\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if gen, err := readGenerationMarker(dir); gen != 1<<64-1 || err != nil {
		t.Fatalf("readGenerationMarker = %d, %v; want the maximum generation", gen, err)
	}
}

// The boot scan reads the marker of every held server. One server's plant must
// neither stall the scan nor change what is reported for the others: the planted
// set is still held, at generation 0 (which makes the API hydrate it), with a WARN.
func TestHeldServerScansSurviveAPlantedMarkerInOneServer(t *testing.T) {
	seed := func(t *testing.T, scratch, id string) string {
		t.Helper()
		dir := filepath.Join(scratch, id)
		if err := os.MkdirAll(dir, 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "level.dat"), []byte("world"), 0o640); err != nil {
			t.Fatal(err)
		}
		return dir
	}
	for _, plant := range markerPlants() {
		t.Run(plant.name, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, nil)
			h := &capturingSlogHandler{}
			m.WithLogger(slog.New(h))
			if err := writeGeneration(seed(t, m.scratchDir, "a-healthy"), 5); err != nil {
				t.Fatal(err)
			}
			outside := plant.plant(t, filepath.Join(seed(t, m.scratchDir, "b-planted"), generationFile))
			if err := writeGeneration(seed(t, m.scratchDir, "c-healthy"), 9); err != nil {
				t.Fatal(err)
			}
			want := []session.HeldServer{
				{ServerID: "a-healthy", Generation: 5},
				{ServerID: "b-planted", Generation: 0},
				{ServerID: "c-healthy", Generation: 9},
			}

			for name, scan := range map[string]func() []session.HeldServer{
				"the quiesced boot scan":   func() []session.HeldServer { return ScanHeldServers(m.scratchDir, true, slog.New(h)) },
				"the unquiesced boot scan": func() []session.HeldServer { return ScanHeldServers(m.scratchDir, false, slog.New(h)) },
				"the per-Register scan":    m.HeldServers,
			} {
				h.records = nil
				var held []session.HeldServer
				within(t, name, func() { held = scan() })

				if len(held) != len(want) {
					t.Fatalf("%s reported %+v, want %+v", name, held, want)
				}
				for i := range want {
					if held[i] != want[i] {
						t.Errorf("%s reported %+v, want %+v", name, held[i], want[i])
					}
				}
				if !warned(h, "generation marker", "b-planted") {
					t.Errorf("%s did not WARN about the refused marker", name)
				}
				if warned(h, "generation marker", "a-healthy") || warned(h, "generation marker", "c-healthy") {
					t.Errorf("%s warned about a healthy server's marker", name)
				}
			}
			assertUntouched(t, outside)
		})
	}
}

// A launch is refused unless the marker is there, and a planted thing is not a
// marker: the Worker would advertise the set at generation 0, so it is not the
// held-claim token the skipped hydrate relied on. The API answers the refusal by
// replaying the launch with a hydrate.
func TestStartRefusedWhenTheMarkerIsNotARegularFile(t *testing.T) {
	for _, plant := range markerPlants() {
		if plant.name == "not a number" || plant.name == "well-formed but over the size cap" ||
			plant.name == "sparse and huge" {
			// Regular files: the guard's predicate is the marker's presence, not its
			// content, exactly as before.
			continue
		}
		t.Run(plant.name, func(t *testing.T) {
			d := &fakeDriver{}
			m := newManager(t, d, nil)
			dir := seedScratch(t, m, "s1")
			if err := os.Remove(filepath.Join(dir, generationFile)); err != nil {
				t.Fatal(err)
			}
			plant.plant(t, filepath.Join(dir, generationFile))

			var res session.CommandResult
			within(t, "the launch guard", func() { res = m.Handle(context.Background(), startCmd()) })

			if res.Success || res.ErrorCode != session.CommandErrorServerNotFound ||
				!strings.Contains(res.ErrorMessage, "working dir absent") {
				t.Fatalf("StartServer = %+v, want the working-set-absent refusal", res)
			}
			if d.startCount() != 0 {
				t.Fatalf("driver started %d times, want 0", d.startCount())
			}
		})
	}
}

// A snapshot declares the marker's generation as its base. A planted marker
// declares none (0), with a WARN, and the stamp that follows the publish replaces
// the plant with a real marker without touching what it pointed at.
func TestRunningSnapshotDeclaresNoBaseForAPlantedMarkerAndRestampsIt(t *testing.T) {
	for _, plant := range markerPlants() {
		if plant.name == "directory" {
			continue // the stamp cannot replace a directory; covered below
		}
		t.Run(plant.name, func(t *testing.T) {
			tr := &fakeTransfer{gen: 12}
			h := &capturingSlogHandler{}
			m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(tr).WithLogger(slog.New(h))
			dir := seedScratch(t, m, "s1")
			if res := m.Handle(context.Background(), startCmd()); !res.Success {
				t.Fatalf("start = %+v, want success", res)
			}
			marker := filepath.Join(dir, generationFile)
			if err := os.Remove(marker); err != nil {
				t.Fatal(err)
			}
			outside := plant.plant(t, marker)

			var res session.CommandResult
			within(t, "the snapshot", func() { res = m.Handle(context.Background(), snapshotCmd()) })

			if !res.Success {
				t.Fatalf("SnapshotTrigger = %+v, want success", res)
			}
			if got := tr.snapshotBaseGenerations; len(got) != 1 || got[0] != 0 {
				t.Fatalf("declared base generations = %v, want [0]", got)
			}
			if !warned(h, "generation marker", "s1") {
				t.Error("the snapshot did not WARN about the refused marker")
			}
			if info, err := os.Lstat(marker); err != nil || !info.Mode().IsRegular() {
				t.Fatalf("the marker after the stamp: %v, %v; want a regular file", info, err)
			}
			if gen, err := readGenerationMarker(dir); gen != 12 || err != nil {
				t.Fatalf("readGenerationMarker after the stamp = %d, %v; want 12", gen, err)
			}
			assertUntouched(t, outside)
		})
	}
}

// A directory planted at the marker cannot be renamed over, so the write fails: the
// set keeps reading as generation 0 and nothing is declared held.
func TestWriteGenerationFailsOnADirectoryAtTheMarker(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, generationFile), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := writeGeneration(dir, 7); err == nil {
		t.Fatal("writeGeneration over a directory = nil, want an error")
	}
	if gen, err := readGenerationMarker(dir); gen != 0 || err == nil {
		t.Fatalf("readGenerationMarker = %d, %v; want 0 and a refusal", gen, err)
	}
}

// The marker write is a root write into a directory the server owns. Whatever is
// planted at the marker's name is replaced, never written through.
func TestWriteGenerationReplacesAPlantWithoutWritingThroughIt(t *testing.T) {
	for _, plant := range markerPlants() {
		if plant.name == "directory" {
			continue
		}
		t.Run(plant.name, func(t *testing.T) {
			dir := t.TempDir()
			marker := filepath.Join(dir, generationFile)
			outside := plant.plant(t, marker)

			var err error
			within(t, "the marker write", func() { err = writeGeneration(dir, 7) })

			if err != nil {
				t.Fatalf("writeGeneration: %v", err)
			}
			if info, statErr := os.Lstat(marker); statErr != nil || !info.Mode().IsRegular() {
				t.Fatalf("the marker: %v, %v; want a regular file", info, statErr)
			}
			if gen, readErr := readGenerationMarker(dir); gen != 7 || readErr != nil {
				t.Fatalf("readGenerationMarker = %d, %v; want 7", gen, readErr)
			}
			assertUntouched(t, outside)
		})
	}
}

// A link planted at the name the write is about to create its temp under is not
// written through either: the temp is created exclusively, so nothing appears at
// the link's target, outside the working set.
func TestWriteGenerationDoesNotCreateItsTempThroughAPlantedLink(t *testing.T) {
	orig := generationTempSuffix
	t.Cleanup(func() { generationTempSuffix = orig })
	generationTempSuffix = func() string { return "predicted" }

	dir, outsideDir := t.TempDir(), t.TempDir()
	target := filepath.Join(outsideDir, "created-by-the-worker")
	if err := os.Symlink(target, filepath.Join(dir, generationFile+"-predicted")); err != nil {
		t.Fatal(err)
	}

	if err := writeGeneration(dir, 7); err == nil {
		t.Fatal("writeGeneration = nil, want an error: every temp name it could use is taken")
	}

	if entries, err := os.ReadDir(outsideDir); err != nil || len(entries) != 0 {
		t.Fatalf("outside the working set: %v, %v; want nothing created", entries, err)
	}
	if _, err := os.Lstat(filepath.Join(dir, generationFile)); !os.IsNotExist(err) {
		t.Fatalf("the marker: lstat err = %v, want it absent", err)
	}
}

// The sweep that follows a marker write unlinks temp-named entries. A link planted
// under such a name is removed as a link; what it points at survives.
func TestGenerationTempSweepRemovesAPlantedLinkNotItsTarget(t *testing.T) {
	dir := t.TempDir()
	outside := writeOutsideGeneration(t)
	planted := filepath.Join(dir, generationFile+"-planted")
	if err := os.Symlink(outside, planted); err != nil {
		t.Fatal(err)
	}
	plantedDir := filepath.Join(dir, generationFile+"-dir")
	if err := os.Mkdir(plantedDir, 0o700); err != nil {
		t.Fatal(err)
	}

	if err := writeGeneration(dir, 7); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Lstat(planted); !os.IsNotExist(err) {
		t.Fatalf("the planted temp-named link survived the sweep: lstat err = %v", err)
	}
	if _, err := os.Lstat(plantedDir); err != nil {
		t.Fatalf("the sweep removed a directory: %v", err)
	}
	assertUntouched(t, outside)
}

// What the hardening above does NOT establish (issue #3336, SECURITY.md Section 6):
// a regular file holding a well-formed number is indistinguishable from a marker
// the Worker wrote, so a server that rewrites its own marker is believed. This pins
// the two places the value goes, so that the trust-model statement cannot drift
// from the code silently.
func TestAForgedWellFormedMarkerIsBelieved(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(tr)
	dir := seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	// What the server does from inside the container: unlink the Worker's marker
	// (it owns the directory) and create its own.
	marker := filepath.Join(dir, generationFile)
	if err := os.Remove(marker); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(marker, []byte("999"), 0o644); err != nil {
		t.Fatal(err)
	}

	if held := m.HeldServers(); len(held) != 1 || held[0].Generation != 999 {
		t.Fatalf("HeldServers = %+v, want the forged generation 999 advertised", held)
	}
	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if got := tr.snapshotBaseGenerations; len(got) != 1 || got[0] != 999 {
		t.Fatalf("declared base generations = %v, want the forged [999]", got)
	}
}
