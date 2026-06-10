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
// generation), and the authoritative-stop scratch GC (issue #762, removeScratch's
// os.RemoveAll over scratchDir/<id>) drops it together with the working set, so a
// GC'd server reports holding nothing and the API hydrates afresh.
const generationFile = ".mcsd_generation"

// writeGeneration records gen as the working-set generation in workingDir. It is
// best-effort from the caller's view: a write failure is returned for logging but
// must not fail the hydrate/snapshot it follows (a missing/stale marker only costs
// an extra hydrate, never correctness). The file is written atomically (temp
// sibling + rename) so a crash mid-write never leaves a torn generation, and the
// temp contents are fsynced before the rename so a crash cannot surface an EMPTY
// marker — a durable rename over unflushed bytes would read as gen 0, and combined
// with the hydrate-merge interplay that "extra hydrate" is not entirely harmless
// (issue #787). The directory is fsynced after the rename so the rename itself is
// durable: the caller (handleHydrate) reaches this only after Hydrate has already
// fsynced the working tree the marker describes, so the marker can never become
// durable before that tree.
func writeGeneration(workingDir string, gen uint64) error {
	// Ensure the working dir exists: a hydrate that served a 204 (no published
	// snapshot) does not create it, but the generation (0) still needs recording so
	// a same-Worker restart re-reports the empty-set generation rather than nothing.
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(workingDir, ".mcsd_generation-*")
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
	if err := os.Rename(tmpName, filepath.Join(workingDir, generationFile)); err != nil {
		return err
	}
	// fsync the dir so the rename (the marker's appearance) is itself durable, not
	// just the file contents: the ordering guarantee (issue #787) requires the
	// marker to become durable only AFTER the tree it describes.
	return fsyncDir(workingDir)
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
