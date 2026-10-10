package instancemanager

import (
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
)

// writeGeneration atomically publishes a fsynced marker, then fsyncs the directory.
// Failures are best-effort, but callers must never stamp a generation newer than the tree they wrote.
func writeGeneration(workingDir string, gen uint64) error {
	return writeGenerationGuarded(workingDir, gen, nil)
}

// Check directory identity immediately before publishing; a replacement before temp creation can otherwise be
// stamped.
// The path-based rename still has a residual race if replacement interleaves with its path resolution.
func writeGenerationGuarded(workingDir string, gen uint64, guard func() bool) error {
	// Ensure the working dir exists: a hydrate that served a 204 (no published
	// snapshot) does not create it, but the generation (0) still needs recording so
	// a same-Worker restart re-reports the empty-set generation rather than nothing.
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		return err
	}
	// The pattern is DERIVED from the marker name, not spelled out: hasWorkingSet, the snapshot pack and
	// sweepGenerationTemps all recognise a temp by that same prefix, so a literal here would let a rename of the
	// constant leave the creation site behind and strand temps no consumer matches.
	tmp, err := os.CreateTemp(workingDir, scratchformat.GenerationMarkerFile+"-*")
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
	if guard != nil && !guard() {
		_ = os.Remove(tmpName)
		return errWorkingDirReplaced
	}
	// A failed rename leaves a complete fsynced temp that consumers ignore.
	// The next successful marker write or whole-scratch cleanup reclaims it.
	if err := os.Rename(tmpName, filepath.Join(workingDir, scratchformat.GenerationMarkerFile)); err != nil {
		return err
	}
	sweepGenerationTemps(workingDir)
	// fsync the dir so the rename (the marker's appearance) is itself durable, not just the file contents: the
	// ordering guarantee requires the marker to become durable only AFTER the tree it describes.
	return fsyncDir(workingDir)
}

// Remove only marker temp files, never directories or the published marker.
// A concurrent writer may lose its best-effort update; the already-published marker remains intact.
func sweepGenerationTemps(workingDir string) {
	entries, err := os.ReadDir(workingDir)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasPrefix(entry.Name(), scratchformat.GenerationMarkerFile+"-") {
			continue
		}
		_ = os.Remove(filepath.Join(workingDir, entry.Name()))
	}
}

// errWorkingDirReplaced reports that a guarded marker write was refused because the
// working dir stopped being the directory the caller pinned. It is a skipped write, not
// a failed one: the caller logs it as such rather than as a marker-write error.
var errWorkingDirReplaced = errors.New("instancemanager: working dir replaced since it was pinned")

// openWorkingDirRef is os.Open, indirected through a package var so a test can force
// the identity capture to fail and pin the fail-toward-skip direction (mirrors
// datatransfer's `var swapRename`). Production always uses os.Open.
var openWorkingDirRef = os.Open

// statWorkingDirRef is the compare-side os.Stat, indirected for the same reason. It is
// what lets a test drive the ONE interleaving the pre-rename guard uniquely closes — a
// replacement landing after the caller's check but before the marker temp is created —
// which is otherwise a microseconds-wide race no test could hit deterministically.
// Production always uses os.Stat.
var statWorkingDirRef = os.Stat

// workingDirRef holds an open descriptor so SameFile cannot accept a recycled inode after replacement.
// Close it on every exit path.
type workingDirRef struct {
	dir  string
	file *os.File    // held open for the whole window — see the ABA note above
	info os.FileInfo // the pinned identity, for os.SameFile
	err  error       // non-nil when the identity could not be captured
}

// pinWorkingDir captures dir's identity. It never returns nil: a capture failure is
// carried in the ref and surfaces from current() as a mismatch, so a caller that cannot
// establish the identity behaves exactly like one that finds it changed.
func pinWorkingDir(dir string) *workingDirRef {
	ref := &workingDirRef{dir: dir}
	f, err := openWorkingDirRef(dir)
	if err != nil {
		ref.err = err
		return ref
	}
	// fstat through the descriptor, not a second path lookup: the identity recorded is
	// then provably the object being held open.
	info, err := f.Stat()
	if err != nil {
		_ = f.Close()
		ref.err = err
		return ref
	}
	ref.file, ref.info = f, info
	return ref
}

// close releases the pinned descriptor. Nil-safe and idempotent-friendly.
func (r *workingDirRef) close() {
	if r == nil || r.file == nil {
		return
	}
	_ = r.file.Close()
}

// current reports whether the path still resolves to the pinned directory, and when it
// does not, a structured reason for the caller's log. Every uncertainty resolves to
// "not current": a false mismatch costs one extra hydrate, while a false match is the
// silent wrong-generation boot this guard exists to prevent.
func (r *workingDirRef) current() (bool, string) {
	if r == nil {
		return false, "identity_unavailable"
	}
	if r.err != nil {
		// A capture that failed because the dir was not there is reported as absent, not
		// as unavailable: the two reasons send an operator looking in different places
		// (a deleted scratch vs. an fd/permission problem), so they must stay disjoint.
		return false, absentOr(r.err, "identity_unavailable")
	}
	now, err := statWorkingDirRef(r.dir)
	if err != nil {
		return false, absentOr(err, "identity_unavailable")
	}
	if !os.SameFile(r.info, now) {
		return false, "working_dir_replaced"
	}
	return true, ""
}

// absentOr classifies a filesystem error as the working dir being gone, falling back to
// otherwise for anything else.
func absentOr(err error, otherwise string) string {
	if os.IsNotExist(err) {
		return "working_dir_absent"
	}
	return otherwise
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

// readGeneration returns the generation recorded in workingDir, or 0 when the marker is absent or unparseable. A
// 0 means "held but at an unknown generation": the API treats it as older than any published store generation
// and hydrates, which is the safe direction (never skip a hydrate on an unknown set).
func readGeneration(workingDir string) uint64 {
	data, err := os.ReadFile(filepath.Join(workingDir, scratchformat.GenerationMarkerFile))
	if err != nil {
		return 0
	}
	gen, err := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 64)
	if err != nil {
		return 0
	}
	return gen
}
