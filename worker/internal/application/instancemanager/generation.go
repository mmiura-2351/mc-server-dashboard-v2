package instancemanager

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// generationFile is the name of the per-server marker file the Worker writes
// inside scratchDir/<server_id> to record the GENERATION its local working set is
// at (issue #763): the authoritative store generation the set was last hydrated
// from or last snapshotted to. It lives INSIDE the scratch dir so it shares the
// scratch's lifecycle — a same-Worker restart retains it (the API re-reports the
// generation), and the post-final-snapshot scratch GC (issue #762/#841,
// removeScratch's os.RemoveAll over scratchDir/<id>) drops it together with the
// working set, so a GC'd server reports holding nothing and the API hydrates afresh.
const generationFile = ".mcsd_generation"

// writeGeneration records gen as the working-set generation in workingDir. It is
// best-effort from the caller's view: a write failure is returned for logging but
// must not fail the hydrate/snapshot it follows. On the 200 hydrate path the marker
// was already written atomically into the temp tree before the swap-in rename
// (issue #917), so this call is idempotent; on the 204 path and the snapshot path a
// missing/stale marker only costs an extra hydrate, never correctness. The file is
// written atomically (temp
// sibling + rename) so a crash mid-write never leaves a torn generation, and the
// temp contents are fsynced before the rename so a crash cannot surface an EMPTY
// marker — a durable rename over unflushed bytes would read as gen 0, and combined
// with the hydrate-merge interplay that "extra hydrate" is not entirely harmless
// (issue #787). The directory is fsynced after the rename so the rename itself is
// durable: the caller (handleHydrate) reaches this only after Hydrate has already
// fsynced the working tree the marker describes, so the marker can never become
// durable before that tree. A successful write also sweeps the temp siblings that
// earlier crashed writes stranded in workingDir (sweepGenerationTemps, issue #2283),
// so the cleanup is self-healing rather than a separate mechanism.
func writeGeneration(workingDir string, gen uint64) error {
	// Ensure the working dir exists: a hydrate that served a 204 (no published
	// snapshot) does not create it, but the generation (0) still needs recording so
	// a same-Worker restart re-reports the empty-set generation rather than nothing.
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		return err
	}
	// The pattern is DERIVED from generationFile, not spelled out: hasWorkingSet
	// (issue #2279), the snapshot pack (issue #834) and sweepGenerationTemps
	// (issue #2283) all recognise a temp by that same prefix, so a literal here
	// would let a rename of the constant leave the creation site behind and strand
	// temps no consumer matches (issue #2287).
	tmp, err := os.CreateTemp(workingDir, generationFile+"-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	if _, err := tmp.WriteString(strconv.FormatUint(gen, 10)); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmpName)
		return err
	}
	// fsync the contents before the rename (the atomicWriteAt idiom in
	// instancemanager.go) so a power loss after the rename cannot surface a
	// zero-length marker.
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmpName)
		return err
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmpName)
		return err
	}
	// A failed rename deliberately leaves the temp in place instead of unlinking it
	// inline as the three paths above do. Those unlink because their temp holds torn or
	// unflushed bytes; this one holds a complete, fsynced generation under the temp
	// form, which every consumer already treats as non-content (issues #2279, #834), so
	// it is inert until reclaimed — by the next successful marker write's sweep
	// (sweepGenerationTemps, issue #2283) or by the scratch GC's RemoveAll, the same two
	// reclaims that cover a crash-stranded temp.
	if err := os.Rename(tmpName, filepath.Join(workingDir, generationFile)); err != nil {
		return err
	}
	sweepGenerationTemps(workingDir)
	// fsync the dir so the rename (the marker's appearance) is itself durable, not
	// just the file contents: the ordering guarantee (issue #787) requires the
	// marker to become durable only AFTER the tree it describes.
	return fsyncDir(workingDir)
}

// sweepGenerationTemps removes the ".mcsd_generation-XXXX" temp siblings a crashed
// earlier marker write left behind in workingDir (issue #2283). A crash between the
// temp write and the rename strands one such file per crash, and nothing else
// reclaims them until the whole scratch dir is GC'd, so the next successful marker
// write cleans up after its predecessors.
//
// It runs AFTER the rename, so the marker itself already carries its final name and
// is never matched: the predicate requires the temp form (the marker name plus "-"),
// not merely the marker prefix that hasWorkingSet (issue #2279) and the snapshot pack
// (issue #834) treat as non-content — those two only IGNORE what they match, while
// this unlinks it, so an over-broad match here would delete real files. Directories
// are skipped for the same reason.
//
// The sweep CAN unlink a CONCURRENT writer's in-flight temp, and does so in practice
// (8 goroutines x 50 writes on one dir: 0 rename errors without the sweep, ~300
// ENOENT renames with it). Two writeGeneration calls do overlap on one workingDir:
// the per-server FIFO lanes are per-STREAM (the dispatcher is recreated per serve,
// domain/session/session.go), and a running-id snapshot deliberately takes no id
// reservation (issue #829 item 4, handleSnapshot) though a hydrate and a stopped-id
// snapshot do, so a dropped stream's post-upload snapshot tail (recordGeneration) can
// still run while a NEW stream's hydrate records its own marker — the same window
// documented as issue #917 item 3.
//
// That is accepted rather than prevented, because what the loser loses is bounded: the
// marker can never be absent or torn (the winner's rename precedes this sweep and its
// final name is never matched), so the loser forfeits only a best-effort marker UPDATE
// — which writeGeneration's contract already permits and recordGeneration logs without
// propagating — and a lost update costs at most one extra hydrate. Which of two
// concurrent writes wins was already last-rename-wins before this sweep existed. The
// sweep is otherwise best-effort too: a ReadDir or Remove failure is ignored so it can
// never fail the marker write.
func sweepGenerationTemps(workingDir string) {
	entries, err := os.ReadDir(workingDir)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasPrefix(entry.Name(), generationFile+"-") {
			continue
		}
		_ = os.Remove(filepath.Join(workingDir, entry.Name()))
	}
}

// fsyncDir fsyncs a directory so a rename/create within it is durable. The dir is
// opened read-only (the only mode a directory fsync needs).
func fsyncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer func() { _ = d.Close() }()
	return d.Sync()
}

// readGeneration returns the generation recorded in workingDir, or 0 when the
// marker is absent or unparseable (issue #763). A 0 means "held but at an unknown
// generation": the API treats it as older than any published store generation and
// hydrates, which is the safe direction (never skip a hydrate on an unknown set).
func readGeneration(workingDir string) uint64 {
	data, err := os.ReadFile(filepath.Join(workingDir, generationFile))
	if err != nil {
		return 0
	}
	gen, err := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 64)
	if err != nil {
		return 0
	}
	return gen
}
