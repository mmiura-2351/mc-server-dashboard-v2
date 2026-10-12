package instancemanager

import (
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"math/rand/v2"
	"os"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/nofollow"
)

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
// (skip_hydrate = held >= store, issue #767) then does not skip. Since issue #2802 an
// ABSENT marker costs that same extra hydrate by a second route: it is the launch
// guard's predicate (launchReserved), so a start the API did skip the hydrate for is
// refused and the API replays the launch WITH the hydrate (issue #2499's replay). Both
// routes converge on one extra transfer, so the bound is unchanged — but a scratch that
// can never persist the marker now blocks the start rather than booting a fresh world,
// which is the correct direction and operator-visible. A marker NEWER than
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
// immediately before and again after the rename that publishes the marker. It exists for the running-id
// snapshot's stamp (issue #2284), whose caller must not publish a marker once the
// working dir has stopped being the directory it packed; every other caller passes nil
// and gets writeGeneration's behaviour byte for byte.
//
// Every step below the working-dir open acts on that one descriptor (issue #3336): the
// directory is owned and written by the server's own code, and this is a root write into
// it. The temp is created exclusively and without following a link, so a link planted at
// its name is refused rather than written through; the rename replaces whatever sits at
// the marker's name — a link included — instead of writing through it; and the sweep
// unlinks names, never what they point at. Nothing is resolved by path below workingDir,
// which is the Worker's own entry in the scratch root and so not the server's to replace.
//
// The guard runs HERE rather than only in the caller because the caller's check is
// check-then-act. Take T to be the instant a concurrent hydrate's swap completes:
//
//   - T before the open below: the descriptor is the REPLACEMENT directory, and without
//     the guard the marker would publish cleanly onto a tree this caller never packed.
//     The guard before the rename refuses it. This is the wrong-stamp window.
//   - T after the open, before that guard: the temp is in the pinned directory, which the
//     swap has moved to .displaced-<id>. The guard refuses and the temp is unlinked there.
//   - T after that guard, before the rename: the marker is published into the pinned
//     directory, now at .displaced-<id> — the tree this caller packed, so the stamp is
//     true of the tree it lands on, and the slot keeps the hydrate's own marker. What
//     would be wrong is REPORTING it: the caller declares the generation as held, and the
//     slot holds another tree. The guard is therefore run once more after the rename and
//     the write is reported as refused. (STORAGE.md Section 4.6's manual recovery reads
//     that displaced marker; it then names the generation that tree was published as.)
//   - T after the second guard: the marker was in the slot's tree when it was checked,
//     and the hydrate that replaced it since records its own.
//
// A refused write removes its temp — unlike the rename-failure path below, which strands
// a complete marker on purpose, this temp is one nothing will ever publish.
func writeGenerationGuarded(workingDir string, gen uint64, guard func() bool) error {
	// Ensure the working dir exists: a hydrate that served a 204 (no published
	// snapshot) does not create it, but the generation (0) still needs recording so
	// a same-Worker restart re-reports the empty-set generation rather than nothing.
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		return err
	}
	dirFd, err := nofollow.OpenDir(workingDir)
	if err != nil {
		return err
	}
	defer func() { _ = unix.Close(dirFd) }()

	tmpName, tmp, err := createGenerationTemp(dirFd)
	if err != nil {
		return err
	}
	if _, err := tmp.WriteString(strconv.FormatUint(gen, 10)); err != nil {
		_ = tmp.Close()
		_ = unix.Unlinkat(dirFd, tmpName, 0)
		return err
	}
	// fsync the contents before the rename (the atomicWriteAt idiom in
	// instancemanager.go) so a power loss after the rename cannot surface a
	// zero-length marker.
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		_ = unix.Unlinkat(dirFd, tmpName, 0)
		return err
	}
	if err := tmp.Close(); err != nil {
		_ = unix.Unlinkat(dirFd, tmpName, 0)
		return err
	}
	if guard != nil && !guard() {
		_ = unix.Unlinkat(dirFd, tmpName, 0)
		return errWorkingDirReplaced
	}
	// A failed rename deliberately leaves the temp in place instead of unlinking it
	// inline as the three paths above do. Those unlink because their temp holds torn or
	// unflushed bytes; this one holds a complete, fsynced generation under the temp
	// form, which every consumer already treats as non-content (issues #2279, #834), so
	// it is inert until reclaimed — by the next successful marker write's sweep
	// (sweepGenerationTemps, issue #2283) or by the scratch GC's RemoveAll, the same two
	// reclaims that cover a crash-stranded temp.
	if err := unix.Renameat(dirFd, tmpName, dirFd, scratchformat.GenerationMarkerFile); err != nil {
		return err
	}
	if guard != nil && !guard() {
		return errWorkingDirReplaced
	}
	sweepGenerationTemps(dirFd)
	// fsync the dir so the rename (the marker's appearance) is itself durable, not
	// just the file contents: the ordering guarantee (issue #787) requires the
	// marker to become durable only AFTER the tree it describes.
	return unix.Fsync(dirFd)
}

// generationTempSuffix names one marker temp. It is a variable so a test can predict
// the name and plant something at it; production never reassigns it.
var generationTempSuffix = func() string { return strconv.FormatUint(rand.Uint64(), 36) }

// createGenerationTemp creates a marker temp in the directory behind dirFd and returns
// its name and the open file. O_EXCL makes the create fail on ANY existing entry and
// O_NOFOLLOW keeps it from resolving a link, so the file written is always one this
// call created, inside that directory. A taken name is retried under another.
//
// The name is DERIVED from the marker name, not spelled out: hasWorkingSet (issue
// #2279), the snapshot pack (issue #834) and sweepGenerationTemps (issue #2283) all
// recognise a temp by that same prefix, so a literal here would let a rename of the
// constant leave the creation site behind and strand temps no consumer matches (issue
// #2287).
func createGenerationTemp(dirFd int) (string, *os.File, error) {
	var name string
	var err error
	for range 16 {
		name = scratchformat.GenerationMarkerFile + "-" + generationTempSuffix()
		var fd int
		fd, err = nofollow.OpenAt(dirFd, name,
			unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0o600)
		if err == nil {
			return name, os.NewFile(uintptr(fd), name), nil
		}
		if !errors.Is(err, unix.EEXIST) {
			break
		}
	}
	return "", nil, &os.PathError{Op: "create", Path: name, Err: err}
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
// are skipped for the same reason. Each name is unlinked relative to the directory's
// descriptor, so a link planted under a temp name is removed as a link and what it
// points at is untouched (issue #3336).
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
// snapshot tail's identity guard (issue #2284) is a concurrent hydrate that did NOT
// replace the working dir — the 204 path leaves destDir untouched. (One that did
// replace it leaves the two writers in different directories, issue #3336.)
//
// That is accepted rather than prevented, because what the loser loses is bounded: the
// marker can never be absent or torn (the winner's rename precedes this sweep and its
// final name is never matched), so the loser forfeits only a best-effort marker UPDATE
// — which writeGeneration's contract already permits and recordGeneration logs without
// propagating — and a lost update costs at most one extra hydrate. Which of two
// concurrent writes wins was already last-rename-wins before this sweep existed. The
// sweep is otherwise best-effort too: a listing or unlink failure is ignored so it can
// never fail the marker write.
func sweepGenerationTemps(dirFd int) {
	dup, err := unix.Dup(dirFd)
	if err != nil {
		return
	}
	dir := os.NewFile(uintptr(dup), ".")
	defer func() { _ = dir.Close() }()
	for {
		// Listed in batches: the server decides how many entries its working-set root
		// has, and the listing must not be held in memory whole.
		names, err := dir.Readdirnames(256)
		for _, name := range names {
			if strings.HasPrefix(name, scratchformat.GenerationMarkerFile+"-") {
				// Without AT_REMOVEDIR this fails on a directory, which is the skip.
				_ = unix.Unlinkat(dirFd, name, 0)
			}
		}
		if err != nil || len(names) == 0 {
			return
		}
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

// maxGenerationMarkerBytes is the largest marker a read accepts. A generation is a
// uint64 in decimal — at most 20 digits — so 32 bytes leaves room for the trailing
// newline a hand-written marker carries and nothing else.
const maxGenerationMarkerBytes = 32

// readGenerationMarker returns the generation recorded in workingDir.
//
// An absent marker (or working dir) is 0 with a nil error: a set that was never stamped
// (issue #763). Anything else that is not a regular file of at most
// maxGenerationMarkerBytes holding one decimal uint64 is REFUSED: 0 with a non-nil
// error. The marker sits in a directory the server's own code owns and can write (issue
// #3336), so it is opened relative to the working dir's descriptor without following a
// link or blocking, judged on the opened descriptor and read up to the cap (nofollow):
// a link to another file is never read, a FIFO never blocks the caller, and an endless
// or huge file costs 33 bytes.
//
// Both outcomes are generation 0, which means "held but at an unknown generation": the
// API treats it as older than any published store generation and hydrates, which is the
// safe direction (never skip a hydrate on an unknown set).
func readGenerationMarker(workingDir string) (uint64, error) {
	dirFd, err := nofollow.OpenDir(workingDir)
	if errors.Is(err, fs.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("open working dir: %w", err)
	}
	defer func() { _ = unix.Close(dirFd) }()

	data, err := nofollow.ReadFileAt(dirFd, scratchformat.GenerationMarkerFile, maxGenerationMarkerBytes)
	if errors.Is(err, fs.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("%s %w", scratchformat.GenerationMarkerFile, err)
	}
	gen, err := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 64)
	if err != nil {
		// Neither %w nor the content: the bytes are the server's.
		return 0, fmt.Errorf("%s does not hold a generation", scratchformat.GenerationMarkerFile)
	}
	return gen, nil
}

// recordedGeneration is readGenerationMarker for the callers that act on the value —
// the held-set scans and the snapshot's base declaration. A refused marker is logged
// and read as 0, exactly like an absent one, so one server's planted marker costs that
// server a hydrate and nothing else.
func recordedGeneration(workingDir, serverID string, log *slog.Logger) uint64 {
	gen, err := readGenerationMarker(workingDir)
	if err != nil && log != nil {
		log.Warn("generation marker refused; treating the working set as generation 0, which forces a hydrate",
			"server_id", serverID, "error", err)
	}
	return gen
}

// generationMarkerPresent reports whether workingDir holds a generation marker: a
// regular file under exactly that name, judged without following a link. It is the
// launch guard's predicate (launchReserved, issue #2802). Anything else at the name is
// not a marker — readGenerationMarker refuses it — so it is not the held claim either.
// A nil error is "present"; otherwise the error says why not, and satisfies
// errors.Is(err, fs.ErrNotExist) when the marker or the working dir is simply absent.
func generationMarkerPresent(workingDir string) error {
	dirFd, err := nofollow.OpenDir(workingDir)
	if err != nil {
		return err
	}
	defer func() { _ = unix.Close(dirFd) }()
	var st unix.Stat_t
	if err := unix.Fstatat(dirFd, scratchformat.GenerationMarkerFile, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG {
		return nofollow.ErrNotRegular
	}
	return nil
}
