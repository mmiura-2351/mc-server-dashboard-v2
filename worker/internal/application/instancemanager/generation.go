package instancemanager

import (
	"errors"
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
// (issue #917), so this call is idempotent; on the 204 path and the snapshot path it
// is the only write.
//
// "Best-effort" is bounded by DIRECTION, and the two are not symmetric (issue #2284).
// A marker OLDER than the tree it sits in — the outcome of a missing or failed write —
// costs one extra hydrate and nothing else, because the API's skip-hydrate gate
// (skip_hydrate = held >= store, issue #767) then does not skip. A marker NEWER than
// its tree is a correctness failure: the gate skips the hydrate that would correct the
// tree and the Worker boots a working set that is not the generation it claims to be.
// So a caller that cannot prove the marker still describes the directory it is writing
// into must not write it at all — see writeGenerationGuarded. The file is
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
	return writeGenerationGuarded(workingDir, gen, nil)
}

// writeGenerationGuarded is writeGeneration with an optional last-moment guard, run
// immediately before the rename that publishes the marker. It exists for the running-id
// snapshot's stamp (issue #2284), whose caller must not publish a marker once the
// working dir has stopped being the directory it packed; every other caller passes nil
// and gets writeGeneration's behaviour byte for byte.
//
// The guard runs THERE rather than only in the caller because the caller's check is
// check-then-act: between it and the rename this function does MkdirAll, CreateTemp,
// WriteString and an fsync, and that fsync is milliseconds to tens of milliseconds
// under load — a wide window, not a negligible one. Re-checking against the rename
// narrows the residual to a single rename syscall. It does NOT close it: the only
// fully atomic form is renameat against the pinned descriptor, which is deliberately
// not done here (see recordGenerationIfUnchanged).
//
// A refused write removes its temp — unlike the rename-failure path below, which
// strands a complete marker on purpose, this temp is one nothing will ever publish.
func writeGenerationGuarded(workingDir string, gen uint64, guard func() bool) error {
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
	if guard != nil && !guard() {
		_ = os.Remove(tmpName)
		return errWorkingDirReplaced
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
// ENOENT renames with it; that measurement drove writeGeneration directly). Two marker
// writes still overlap on one workingDir, though the shape is narrower than it was:
// the per-server FIFO lanes are per-STREAM (the dispatcher is recreated per serve,
// domain/session/session.go), and a running-id snapshot deliberately takes no id
// reservation (issue #829 item 4, handleSnapshot) though a hydrate and a stopped-id
// snapshot do, so a dropped stream's post-upload snapshot tail can still run while a
// NEW stream's hydrate records its own marker. What survives of that overlap after the
// snapshot tail's identity guard (issue #2284) is (i) a concurrent hydrate that did NOT
// replace the working dir — the 204 path leaves destDir untouched — and (ii) the
// guard's own residual, the rename-sized window between its last check and the marker
// rename.
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

// errWorkingDirReplaced reports that a guarded marker write was refused because the
// working dir stopped being the directory the caller pinned. It is a skipped write, not
// a failed one: the caller logs it as such rather than as a marker-write error.
var errWorkingDirReplaced = errors.New("instancemanager: working dir replaced since it was pinned")

// openWorkingDirRef is os.Open, indirected through a package var so a test can force
// the identity capture to fail and pin the fail-toward-skip direction (mirrors
// datatransfer's `var swapRename`). Production always uses os.Open.
var openWorkingDirRef = os.Open

// workingDirRef pins the IDENTITY of a working directory across a window in which a
// concurrent stream may replace it (issue #2284). It answers one question: is the
// directory at this path still the same directory object it was when the window opened?
//
// TWO mechanics make that answer trustworthy, and NEITHER is an optimisation:
//
//   - os.SameFile compares (device, inode) and is portable, so no syscall/build-tag
//     work — a hand-rolled inode comparison would buy nothing.
//   - The descriptor is held OPEN for the whole window. A bare Stat-at-capture /
//     Stat-at-compare token is defeated by inode ABA: a first hydrate renames the dir
//     aside, a sweep frees the inode, and a second hydrate's os.MkdirTemp can be handed
//     that same inode back and rename it into place — the token then MATCHES and the
//     caller acts on a directory it never pinned. Holding the descriptor defers the
//     inode's reclamation until close, so the number can never be recycled inside the
//     window. Do not "simplify" this to two Stat calls; the double-hydrate case that
//     breaks it is one handleSnapshot explicitly documents as reachable, and
//     TestWorkingDirRefSurvivesUnlinkAndRejectsReplacement fails if the descriptor goes.
//
// The cost is one file descriptor per in-flight running snapshot, so a ref MUST be
// closed on every path out of the window.
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
	if r == nil || r.err != nil {
		return false, "identity_unavailable"
	}
	now, err := os.Stat(r.dir)
	if err != nil {
		if os.IsNotExist(err) {
			return false, "working_dir_absent"
		}
		return false, "identity_unavailable"
	}
	if !os.SameFile(r.info, now) {
		return false, "working_dir_replaced"
	}
	return true, ""
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
