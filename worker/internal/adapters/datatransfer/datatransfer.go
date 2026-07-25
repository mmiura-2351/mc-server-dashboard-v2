// Package datatransfer is the Worker's HTTP data-plane client: it moves a
// server's working set between the API's authoritative Storage and the local
// scratch dir (FR-DATA-3, FR-DATA-4). The control plane only triggers a
// transfer and hands over a URL + token (CONTROL_PLANE.md Section 5); this
// adapter does the bulk byte movement, off the gRPC stream.
//
//   - Hydrate: GET the working-set tar and stream-unpack it into the instance
//     working dir. Members are path-sanitized (absolute paths and "..", and any
//     symlink/hardlink escape, are rejected), mirroring the API-side filter="data"
//     discipline so a hostile archive cannot escape the working dir. A 204 No
//     Content means the server has no published working set yet; the Worker treats
//     it as an empty dir and launches fresh.
//   - Snapshot: pack the working dir into a tar spooled to a temp file (so RAM
//     stays bounded for multi-GB worlds), Stat it for a Content-Length, then
//     stream the file as the request body so the API's "proven complete" gate
//     can verify the streamed byte count (STORAGE.md Section 4.1, FR-DATA-6).
//
// Transport security mirrors the control channel (CONFIGURATION.md Section 6.1):
// the same CA bundle / mTLS / insecure-dev posture is reused via the injected
// *http.Client built in the wiring layer. The transfer token travels as
// "Authorization: Bearer <token>", the same credential model as the stream.
package datatransfer

import (
	"archive/tar"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// generationMarkerFile is the Worker-private marker at the working-set root (inside
// scratchDir/<id>, NOT its parent) recording the local generation (issue #763). THIS
// ADAPTER writes it on the 200 hydrate path: unpackAndSwap puts it into the temp tree
// before the swap-in rename so it is atomic with the new destDir (issue #917). That
// write is a correctness dependency, not a corrective touch-up — a destDir with no
// marker reads as generation 0, so the API re-dispatches hydrate and that spurious
// retry discards this working set whenever a .displaced-<id> is retained (issue
// #2278). writeGeneration still covers the
// 204 and snapshot paths (and is idempotent on the 200 path). The marker is excluded
// from a snapshot pack so this Worker-private state never lands in the authoritative
// stored working set (and is never re-hydrated to another Worker or the live
// Minecraft dir). Kept as a local constant to avoid the adapter depending on the
// instancemanager package.
const generationMarkerFile = ".mcsd_generation"

// generationHeader is the response header the API data plane stamps on a hydrate
// (the store generation served) and a snapshot (the new store generation
// published) so the Worker can record the generation of its local working set
// (issue #763). An absent or unparseable header is read as generation 0.
const generationHeader = "X-Working-Set-Generation"

// baseGenerationHeader is the REQUEST header the Worker stamps on a snapshot
// publish with the store generation its working set was hydrated from (issue
// #847). The API's publish-time generation guard refuses the publish if the store
// has since advanced past it, preventing a stale set from clobbering a newer
// authoritative copy. Omitted when 0 (an unknown/never-hydrated set): the guard
// then has no base to compare and the publish proceeds as before.
const baseGenerationHeader = "X-Working-Set-Base-Generation"

// workerIDHeader is the REQUEST header the Worker stamps on a snapshot publish with
// its own id (issue #847 bug 3), recorded by the API alongside the generation so the
// guard can tell a same-Worker re-publish (lost-response self-heal) from a
// different-Worker stale-scratch publish (A->B->A). Omitted when empty (an
// unconfigured Worker): the guard then treats the publisher as unknown and stays
// permissive.
const workerIDHeader = "X-Worker-Id"

// parseGeneration reads the store generation from a response header, returning 0
// when it is absent or unparseable (the safe direction: the API treats 0 as older
// than any published store generation and re-hydrates).
func parseGeneration(h http.Header) uint64 {
	gen, err := strconv.ParseUint(h.Get(generationHeader), 10, 64)
	if err != nil {
		return 0
	}
	return gen
}

// Client moves working sets over the API HTTP data plane. It is safe for
// concurrent use (it holds only an *http.Client and a *slog.Logger).
type Client struct {
	http   *http.Client
	logger *slog.Logger
}

// New builds a Client over the given *http.Client (built with the control
// channel's TLS posture in the wiring layer).
func New(httpClient *http.Client) *Client {
	return &Client{http: httpClient, logger: slog.Default()}
}

// WithLogger sets the logger used for pack-time observability (cap/pad
// adjustments and vanished-file skips). The default is slog.Default(). l must
// not be nil; pass slog.Default() explicitly if no custom logger is available.
func (c *Client) WithLogger(l *slog.Logger) *Client {
	if l == nil {
		l = slog.Default()
	}
	c.logger = l
	return c
}

// Hydrate downloads the working-set tar from url into destDir, REPLACING its
// contents wholesale: the tar is unpacked into a fresh temp sibling that is then
// atomically swapped into destDir, so a retained stale working set is replaced
// (not merged) and any symlink a previous run planted in destDir is never
// traversed (issue #772). The generation marker is written into the temp tree
// before the swap-in rename so it is atomic with the new destDir (issue #917);
// the caller's recordGeneration call is still needed for the 204 path and is
// idempotent on the 200 path. A 204 response
// means "no published working set"; destDir is left empty and Hydrate returns nil
// (the Worker launches against an empty dir). Any archive member that would
// escape destDir is rejected and aborts the transfer.
func (c *Client) Hydrate(ctx context.Context, url, token, destDir string) (uint64, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0, fmt.Errorf("datatransfer: build hydrate request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("datatransfer: hydrate request: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	switch resp.StatusCode {
	case http.StatusNoContent:
		// No published working set yet; nothing to unpack. The store generation is
		// 0 (the API serves no generation header on a 204), so the Worker records 0.
		//
		// IMPLICIT CALLER DEPENDENCY: this returns WITHOUT touching destDir, so a
		// retained stale destDir from a prior placement is left in place (not
		// replaced). That is safe only because the caller never reaches a 204 with a
		// stale destDir to displace: store generation 0 + a held working set means the
		// API gates this off with skip_hydrate (lifecycle.py), so a 204 here only ever
		// hydrates onto an empty/absent destDir. Leaving the retained destDir is
		// intentional — do not add a blind destDir wipe here.
		return parseGeneration(resp.Header), nil
	case http.StatusOK:
	default:
		return 0, fmt.Errorf("datatransfer: hydrate: unexpected status %s", resp.Status)
	}

	gen := parseGeneration(resp.Header)
	if err := unpackAndSwap(resp.Body, destDir, gen, c.logger); err != nil {
		return 0, fmt.Errorf("datatransfer: unpack: %w", err)
	}
	// The store generation the API served, recorded by the caller alongside the
	// freshly unpacked working set (issue #763). The marker was already written into
	// the temp tree before the swap-in rename (issue #917), so this return value is
	// still used by the caller's recordGeneration for the 204 path and is idempotent
	// on the 200 path.
	return gen, nil
}

// snapshotSpoolPrefix is the temp-file prefix Snapshot uses for its tar spool in
// the scratch root. SweepSnapshotSpools matches it at startup to reclaim spools a
// crash mid-snapshot left behind.
const snapshotSpoolPrefix = "snapshot-"

// SweepSnapshotSpools removes snapshot-*.tar spool files a crash mid-Snapshot left
// in scratchRoot (issue #787). Snapshot spools its tar to a temp file there and
// removes it on every return path, but a worker death between create and that
// deferred remove leaks a world-sized file permanently: ScanHeldServers only walks
// directories, so the orphan is invisible while consuming disk per crash. This runs
// at startup alongside the held-server scan (cmd/worker/main.go). It is best-effort:
// an unreadable root or a failed remove is ignored (a leftover is wasted disk, never
// a correctness problem). Only top-level files matching the spool prefix and .tar
// suffix are touched, so a server's working-set subdir is never entered.
func SweepSnapshotSpools(scratchRoot string) {
	entries, err := os.ReadDir(scratchRoot)
	if err != nil {
		return
	}
	for _, e := range entries {
		name := e.Name()
		if !e.IsDir() && strings.HasPrefix(name, snapshotSpoolPrefix) && strings.HasSuffix(name, ".tar") {
			_ = os.Remove(filepath.Join(scratchRoot, name))
		}
	}
}

// PackSnapshot packs srcDir into a tar spooled to a temp file in srcDir's parent
// (the scratch root, so it shares srcDir's filesystem). It returns the spool path
// and a cleanup function that removes the spool. The caller can release the quiesce
// bracket after PackSnapshot returns because only the pack reads the working
// directory; the upload reads only the spool (issue #1710). A crash before the
// cleanup leaks the spool; SweepSnapshotSpools reclaims such leftovers at startup
// (issue #787).
func (c *Client) PackSnapshot(_ context.Context, srcDir string) (string, func(), error) {
	spool, err := os.CreateTemp(filepath.Dir(srcDir), snapshotSpoolPrefix+"*.tar")
	if err != nil {
		return "", func() {}, fmt.Errorf("datatransfer: create snapshot spool: %w", err)
	}
	spoolPath := spool.Name()
	if err := packTar(srcDir, spool, c.logger); err != nil {
		_ = spool.Close()
		_ = os.Remove(spoolPath)
		return "", func() {}, fmt.Errorf("datatransfer: pack: %w", err)
	}
	_ = spool.Close()
	cleanup := func() { _ = os.Remove(spoolPath) }
	return spoolPath, cleanup, nil
}

// UploadSnapshot streams the tar spool file at spoolPath to url, declaring
// baseGeneration and workerID for the API's publish-time generation guard (issue
// #847). It returns the NEW store generation the publish produced (the value of the
// API's response header, issue #763); 0 when the header is absent (an older API).
func (c *Client) UploadSnapshot(ctx context.Context, url, token, spoolPath string, baseGeneration uint64, workerID string) (uint64, error) {
	f, err := os.Open(spoolPath)
	if err != nil {
		return 0, fmt.Errorf("datatransfer: open snapshot spool: %w", err)
	}
	defer func() { _ = f.Close() }()

	info, err := f.Stat()
	if err != nil {
		return 0, fmt.Errorf("datatransfer: stat snapshot spool: %w", err)
	}
	size := info.Size()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, f)
	if err != nil {
		return 0, fmt.Errorf("datatransfer: build snapshot request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/x-tar")
	if baseGeneration != 0 {
		req.Header.Set(baseGenerationHeader, strconv.FormatUint(baseGeneration, 10))
	}
	if workerID != "" {
		req.Header.Set(workerIDHeader, workerID)
	}
	req.ContentLength = size

	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("datatransfer: snapshot request: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("datatransfer: snapshot: unexpected status %s", resp.Status)
	}
	return parseGeneration(resp.Header), nil
}

// Snapshot packs srcDir into a tar and uploads it to url in one step. It composes
// PackSnapshot + UploadSnapshot for callers that do not need to release a quiesce
// bracket between pack and upload (e.g. the stopped-id path and e2e tests).
func (c *Client) Snapshot(ctx context.Context, url, token, srcDir string, baseGeneration uint64, workerID string) (uint64, error) {
	spoolPath, cleanup, err := c.PackSnapshot(ctx, srcDir)
	if err != nil {
		return 0, err
	}
	defer cleanup()
	return c.UploadSnapshot(ctx, url, token, spoolPath, baseGeneration, workerID)
}

// unpackAndSwap unpacks the tar stream into a fresh temp sibling of destDir, then
// atomically swaps it into place (issue #772). Unpacking into a brand-new tree —
// rather than over the retained scratch — gives REPLACE semantics (files deleted
// upstream do not survive) and means a symlink a previous run planted in destDir
// is never traversed (the destination tree has no pre-existing entries). The
// generation marker is written into the temp tree BEFORE the swap-in rename
// (issue #917) so it is atomic with the new destDir (issue #763).
//
// The temp dir is a dot-prefixed sibling in destDir's parent (the scratch root).
// ScanHeldServers (scratchscan.go) skips the .displaced-<id> sibling and never
// reports a .hydrate-* temp leftover as a held server it assigned, so a
// crash-leftover .hydrate-* sibling is never matched; a stale one is also reclaimed
// by the next hydrate's leftover sweep below (if the id is re-placed here) and by
// the post-final-snapshot scratch GC, which sweeps this id's .hydrate-<id>-* siblings
// alongside removing scratchDir/<id> once the stopped-id final snapshot publishes
// (issue #766/#841/#842, instancemanager.removeScratch).
//
// Displaced-tree retention (issue #906): the old working set this hydrate replaces
// is NOT deleted — it is renamed aside to the per-server .displaced-<id> sibling.
// When the final stop snapshot definitively failed (e.g. refused by an integrity
// gate, #905), #845 retained the scratch precisely so the only copy of the world
// survives; deleting it here on the next start's hydrate would destroy that copy.
// Moving it aside keeps it recoverable by an operator after such an incident. The
// displaced tree is dot-prefixed so it is never mistaken for a live scratch:
// ScanHeldServers skips the .displaced-<id> prefix (scratchscan.go) so it is never
// reported as a held server. At most one displaced tree exists per server, and it is
// GC'd on the next SUCCESSFUL snapshot for this id, the moment the store provably
// supersedes it (instancemanager.sweepDisplaced, mirroring the #845 GC-on-success
// pattern).
//
// OLDEST-WINS when the slot is already occupied (issue #2278): a hydrate that finds a
// .displaced-<id> already there KEEPS it and discards the working set it just displaced.
// The rule rests on one provable fact — every successful snapshot for this id calls
// sweepDisplaced, so a surviving .displaced-<id> means ZERO successful snapshots since it
// was created. Both trees are therefore unpublished branches, and the discarded one can be
// strictly NEWER; the swap emits a WARN naming both paths because of that. The rationale
// and the rejected alternatives are in the swap block below and in issue #2278.
//
// Crash safety (park-aside-first swap): the temp tree is built fully (including the
// generation marker, issue #917) before any rename. When a live destDir is present, the
// swap then does, in order, (1) park the live destDir aside — DIRECTLY at .displaced-<id>
// when that slot is free, so the recovery copy is never left under a .hydrate-<id>-* name
// the NEXT hydrate's sweepHydrateLeftovers would delete (issue #910); at a
// .hydrate-<id>-superseded-* name when the slot is occupied, because oldest-wins retains
// what is already there and this set is the one elected to be dropped — and (2) rename
// temp -> destDir. On a (2) failure the parked set is renamed straight back, so the
// failure loses nothing. On success the parked set is deleted only in the
// slot-was-occupied case (best-effort).
//
// A crash between (1) and (2) leaves destDir absent but every copy on disk: the parked
// set, any retained .displaced-<id>, and the new tree at the temp name. Nothing is lost —
// the next start re-hydrates (the missing destDir reports as "holding nothing"), the temp
// and superseded leftovers are swept, and the retained .displaced-<id> tree, which no
// hydrate-time sweep touches, stays recoverable until the next SUCCESSFUL snapshot GCs it.
// That converges on exactly the state a clean run produces.
//
// A retained .displaced-<id> is never renamed and never unlinked by this path, so its
// survival needs NO fsync at all: no power loss anywhere in the swap can roll it into a
// missing state. Only the parked-aside rename and the swap-in depend on the fsyncDir
// below. Invariant: from the moment destDir is parked aside, the world it held always
// exists under some name until the swap-in provably succeeds.
//
// Nothing at .displaced-<id> is touched unless a live destDir exists to displace. If
// destDir is ABSENT (this very crash window from a prior interrupted hydrate), the
// existing .displaced-<id> may be the ONLY copy of the world; this hydrate has nothing to
// displace and leaves it untouched, so re-running the interrupted hydrate never destroys
// the recovery copy (issue #910).
//
// Generation marker atomicity (issue #917): the generation marker is written into the
// temp tree BEFORE the swap-in rename so it is atomic with the new destDir. A crash
// after swap-in but before a post-swap marker write would leave a destDir with no
// marker — the API reads gen 0 and re-dispatches hydrate, and that spurious retry
// discards this working set whenever a .displaced-<id> is retained (issue #2278).
// Writing it pre-swap closes that window.
func unpackAndSwap(r io.Reader, destDir string, gen uint64, log *slog.Logger) error {
	parent := filepath.Dir(destDir)
	if err := os.MkdirAll(parent, 0o750); err != nil {
		return err
	}
	// Reclaim any temp/trash siblings a previous crashed hydrate left behind before
	// allocating new ones, so they never accumulate.
	sweepHydrateLeftovers(parent, filepath.Base(destDir))

	tmpDir, err := os.MkdirTemp(parent, hydrateTmpPrefix(destDir)+"*")
	if err != nil {
		return err
	}
	// Best-effort cleanup of the temp tree: harmless once it has been renamed into
	// place (RemoveAll on a now-absent path is a no-op).
	defer func() { _ = os.RemoveAll(tmpDir) }()

	if err := unpackTar(r, tmpDir); err != nil {
		return err
	}

	// Write the generation marker into the temp tree BEFORE the swap-in rename
	// (issue #917): the marker must be atomic with the new destDir so a crash after
	// swap-in never leaves a destDir with no marker. Without this, the API reads
	// gen 0, re-dispatches hydrate, and the retry's RemoveAll destroys the recovery
	// copy. writeFile fsyncs the contents; fsyncTree below makes the dir entry durable.
	if err := writeFile(filepath.Join(tmpDir, generationMarkerFile),
		strings.NewReader(strconv.FormatUint(gen, 10)), 0o640); err != nil {
		return err
	}

	// Durability ordering (issue #787): make the fully built temp tree durable
	// BEFORE the swap renames. unpackTar already fsynced each file's contents; this
	// fsyncs every directory in the tree so the dir entries (the names pointing at
	// those files) are durable too. A power loss after the swap must never persist
	// the new destDir and the generation marker over a tree whose files or names are
	// not yet on disk — the #767 skip gate would boot that torn world.
	if err := fsyncTree(tmpDir); err != nil {
		return err
	}

	// Displace-first swap (issue #906/#910/#917): move the old working set ASIDE
	// BEFORE swapping the new tree in. When the .displaced-<id> slot is free the aside
	// name IS that recovery name, so the old world is never parked under an intermediate
	// trash name a later sweep would delete. The displaced tree is the only copy of the
	// world whenever the final stop snapshot definitively failed and #845 retained the
	// scratch for recovery; it is GC'd only on the next SUCCESSFUL snapshot
	// (instancemanager.sweepDisplaced).
	//
	// Superseded-set deferral (issue #917 bug 2, #2278): the live working set is parked
	// ASIDE, never deleted, before the swap-in. Whichever name it is parked under, a
	// swap-in failure renames it straight back, so no path deletes a world before the
	// replacement is provably in place.
	//
	// Disk cost of that deferral: THREE world-sized copies of this one server — the
	// unpacked temp tree, the retained .displaced-<id>, and the live set parked aside
	// until the swap-in succeeds — are live ACROSS the swap. Scratch capacity planning
	// must budget for it through swap completion (STORAGE.md Section 4.6).
	displaced := displacedDir(destDir)
	asideAt := ""      // where the live destDir was parked; empty when there was nothing to displace
	dropAside := false // true when asideAt is the sweepable name, i.e. an older displaced tree is being kept
	if _, err := os.Lstat(destDir); err == nil {
		// A live working set is present to displace.
		//
		// OLDEST-WINS (issue #2278). When .displaced-<id> is already occupied, the
		// existing tree is KEPT — never renamed, never removed by this path — and the
		// set this hydrate displaces is parked under a sweepable name and dropped once
		// the swap-in succeeds.
		//
		// What that choice rests on, precisely: every SUCCESSFUL snapshot for this id
		// calls sweepDisplaced, so a .displaced-<id> still present at hydrate time proves
		// ZERO successful snapshots for this id since it was created. This branch is
		// therefore narrow — it needs a second displacement with no successful snapshot
		// in between.
		//
		// What it does NOT rest on: it is NOT true that the set being displaced is
		// "merely the store copy" and therefore cheap. It is the store copy PLUS
		// everything Minecraft wrote since, and by the very argument above none of that
		// progression was published either. BOTH trees are unpublished branches and the
		// one dropped here can be strictly NEWER (e.g. an operator's restore_backup bumps
		// the store generation, so the skip gate does not skip, and this hydrate discards
		// days of unsnapshotted play while retaining the older tree). That is the accepted
		// cost of the policy, chosen because the alternative loses the intact world in the
		// torn-world case (#834: a torn destDir advertises generation 0, a hydrate is
		// dispatched, and newest-wins would retain the torn tree over an intact older one).
		// The WARN below names both paths so the cost is never silent.
		//
		// Deliberately NOT health-aware: no fsck, no mtime comparison to pick the "better"
		// tree. That is option C in issue #2278 and was rejected — do not "improve" this
		// into it.
		if info, held := displacedSlotHoldsTree(displaced); held {
			log.Warn("hydrate: an older displaced recovery tree already exists; keeping it and discarding the working set this hydrate replaces (oldest-wins, issue #2278; see STORAGE.md Section 4.6)",
				"server_id", filepath.Base(destDir),
				"retained", displaced,
				"retained_mtime", info.ModTime().UTC().Format(time.RFC3339),
				"discarded", destDir)
			aside, mkErr := os.MkdirTemp(parent, hydrateTmpPrefix(destDir)+"superseded-*")
			if mkErr != nil {
				return mkErr
			}
			// MkdirTemp creates the dir; remove it so Rename can use the name.
			_ = os.Remove(aside)
			asideAt, dropAside = aside, true
		} else {
			// The slot is free (or held only junk, already cleared): take the ordinary
			// displace path, which parks the live set DIRECTLY at .displaced-<id> — never
			// under an intermediate name a later sweep would delete (issue #910).
			asideAt = displaced
		}
		if err := os.Rename(destDir, asideAt); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	if err := swapRename(tmpDir, destDir); err != nil {
		if asideAt != "" {
			// Restore the live working set so the failure does not lose it. If this
			// restore itself fails the set still survives under asideAt, and any retained
			// .displaced-<id> was never touched, so no state here has zero copies.
			//
			// Note this is why the WARN above is phrased as intent: on this path the
			// discard does not actually happen.
			_ = os.Rename(asideAt, destDir)
		}
		return err
	}
	// Swap succeeded. When an older displaced tree was retained instead, the set parked
	// aside is the one the policy elected to drop. Best-effort: a failure here leaks a
	// .hydrate-<id>-* tree that every sweeper reclaims later.
	if dropAside {
		_ = os.RemoveAll(asideAt)
	}
	// fsync the scratch root so BOTH swap renames (the displace-aside and the swap-in)
	// are durable: a power loss must not roll the displace rename back, and the marker
	// the caller writes next (writeGeneration, also fsynced) can then never become
	// durable before the destDir tree it describes (issue #787).
	if err := fsyncDir(parent); err != nil {
		return err
	}
	return nil
}

// displacedSlotHoldsTree reports whether the .displaced-<id> slot holds a retainable
// recovery tree, returning its FileInfo for the discard WARN. Junk in the slot — a
// regular file, a symlink, or an empty directory — is cleared (best-effort) and
// reported as NOT holding a tree.
//
// The guard exists because oldest-wins (issue #2278) reads the slot as a decision, not
// as an obstacle: under the previous newest-wins policy junk was simply overwritten,
// whereas here a bare Lstat success would make the hydrate preserve garbage and discard
// a real world. Realistic sources of junk: a partially-failed best-effort os.RemoveAll
// inside instancemanager.sweepDisplaced, or an operator's half-finished manual cleanup
// (STORAGE.md Section 4.6). Clearing it loses nothing (an empty dir and a non-dir hold
// no world), and it is required anyway: renaming a directory onto an existing FILE fails
// with ENOTDIR, so the ordinary displace path could not proceed otherwise.
//
// A directory that cannot be read is treated as holding a tree, NOT as junk: this
// function must never delete something it has not proven to be empty.
func displacedSlotHoldsTree(displaced string) (os.FileInfo, bool) {
	info, err := os.Lstat(displaced)
	if err != nil {
		return nil, false
	}
	if info.IsDir() {
		d, openErr := os.Open(displaced)
		if openErr != nil {
			return info, true
		}
		defer func() { _ = d.Close() }()
		// One name is enough to decide; io.EOF means the directory is empty.
		if _, readErr := d.Readdirnames(1); !errors.Is(readErr, io.EOF) {
			return info, true
		}
	}
	_ = os.RemoveAll(displaced)
	return nil, false
}

// displacedDir is the per-server path the swap moves a displaced old working set to
// (issue #906): a dot-prefixed sibling of destDir so it cannot collide with a
// server-id scratch dir and is never matched to an assigned id by the API. One per
// server (no random suffix): the name is written only when the slot is free, so exactly
// one displaced tree per server exists at any time (issue #2278).
func displacedDir(destDir string) string {
	return filepath.Join(filepath.Dir(destDir), displacedPrefix+filepath.Base(destDir))
}

// displacedPrefix is the dot-prefixed name prefix for a displaced old working set
// (issue #906), kept aside by a hydrate and GC'd on the next successful snapshot for
// the server (instancemanager.sweepDisplaced).
const displacedPrefix = ".displaced-"

// swapRename is the final temp->destDir swap rename, indirected through a package
// var so a test can force it to fail and exercise the displaced-restore path (the swap
// renames within one parent dir are symmetric, so there is no static-perms way to
// fail only this one). Production always uses os.Rename.
var swapRename = os.Rename

// openFile is the function used by writeRegular to open a file for reading. It
// is indirected through a package var so a test can inject ENOENT for a specific
// path (simulating log-rotation deletion between the walk and the open) without
// needing to race real filesystem timings. Production always uses os.Open.
var openFile = os.Open

// readDir is the function used by walkInto to list a directory. Indirected
// through a package var for the same reason as openFile: a test can inject ENOENT
// for a specific directory (simulating a rotated log dir / plugin temp dir
// deleted between the parent's walk and this read) without racing real timings.
// Production always uses os.ReadDir.
var readDir = os.ReadDir

// entryInfo resolves a DirEntry's FileInfo for walkInto. os.DirEntry.Info() lazily
// lstats the entry, so it returns ENOENT when the entry vanishes between the ReadDir
// walk and this call — the same race family as openFile/readDir (#820/#853/#854).
// Indirected through a package var so a test can inject ENOENT for a specific entry
// without racing real filesystem timings. Production always uses entry.Info().
var entryInfo = func(entry os.DirEntry) (os.FileInfo, error) { return entry.Info() }

// fsyncTree fsyncs every directory in the tree rooted at dir (post-order, so a
// child dir is durable before its parent's entry for it). File contents are already
// fsynced as written (writeFile); this makes the directory entries durable so a
// crash cannot lose a just-created name. Issue #787.
func fsyncTree(dir string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		if e.IsDir() {
			if err := fsyncTree(filepath.Join(dir, e.Name())); err != nil {
				return err
			}
		}
	}
	return fsyncDir(dir)
}

// fsyncDir fsyncs a directory so renames/creates within it are durable. The dir is
// opened read-only (the only mode a directory fsync needs).
func fsyncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer func() { _ = d.Close() }()
	return d.Sync()
}

// hydrateTmpPrefix is the dot-prefixed name prefix for the per-hydrate temp dir,
// derived from destDir's basename so a crash leftover is recognizable and the
// leftover sweep can match it.
func hydrateTmpPrefix(destDir string) string {
	return ".hydrate-" + filepath.Base(destDir) + "-"
}

// sweepHydrateLeftovers removes temp/trash dirs a previous crashed hydrate for the
// same server left in parent. It is best-effort: a removal failure is ignored (the
// stale dir is harmless — ScanHeldServers never matches it to an assigned id).
func sweepHydrateLeftovers(parent, serverID string) {
	entries, err := os.ReadDir(parent)
	if err != nil {
		return
	}
	prefix := ".hydrate-" + serverID + "-"
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), prefix) {
			_ = os.RemoveAll(filepath.Join(parent, e.Name()))
		}
	}
}

// unpackTar extracts a tar stream into destDir, rejecting any member whose
// resolved path escapes destDir (absolute paths, "..", and link targets that
// point outside). This mirrors the API-side filter="data" sandbox.
func unpackTar(r io.Reader, destDir string) error {
	root, err := filepath.Abs(destDir)
	if err != nil {
		return err
	}

	tr := tar.NewReader(r)
	for {
		header, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}

		target, err := safeJoin(root, header.Name)
		if err != nil {
			return err
		}

		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o750); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := os.MkdirAll(filepath.Dir(target), 0o750); err != nil {
				return err
			}
			if err := writeFile(target, tr, os.FileMode(header.Mode)); err != nil {
				return err
			}
		case tar.TypeSymlink, tar.TypeLink:
			// Reject links outright: a symlink/hardlink is the classic escape
			// vector, and a Minecraft working set has no legitimate need for one.
			return fmt.Errorf("datatransfer: refusing link member %q", header.Name)
		default:
			// Skip devices, fifos, and other special members; they are never part
			// of a legitimate working set.
			continue
		}
	}
}

// writeFile creates target, copies the member body into it, and fsyncs the
// contents before close (issue #787): the unpacked tree is swapped into place with
// renames, and a rename only orders metadata — without this fsync a power loss
// could persist the swap (and the generation marker) while a just-written file is
// still all zeros or truncated, and the #767 skip gate would then boot that torn
// world. fsyncing per file as it is written keeps the cost proportional to the data
// already streamed (one extra flush per file, not a re-read of the whole tree); the
// per-dir entries are made durable by a single recursive dir-fsync after unpack.
func writeFile(target string, src io.Reader, mode os.FileMode) error {
	if mode == 0 {
		mode = 0o640
	}
	// O_NOFOLLOW refuses to follow a symlink at the final path component. The
	// unpack target is a brand-new temp tree so no link can pre-exist, but this
	// keeps the write self-defending against any residual link in the destination.
	out, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY|syscall.O_NOFOLLOW, mode.Perm())
	if err != nil {
		return err
	}
	defer func() { _ = out.Close() }()
	if _, err := io.Copy(out, src); err != nil {
		return err
	}
	if err := out.Sync(); err != nil {
		return err
	}
	return out.Close()
}

// safeJoin joins name under root and verifies the result stays inside root.
// Absolute paths and any ".." component are rejected outright (not clamped),
// mirroring the API-side filter="data" discipline; the realpath containment
// check then catches any residual escape.
func safeJoin(root, name string) (string, error) {
	slashed := filepath.ToSlash(name)
	if path.IsAbs(slashed) {
		return "", fmt.Errorf("datatransfer: refusing absolute member %q", name)
	}
	for _, part := range strings.Split(slashed, "/") {
		if part == ".." {
			return "", fmt.Errorf("datatransfer: refusing path escape %q", name)
		}
	}
	joined := filepath.Join(root, filepath.FromSlash(slashed))
	if joined != root && !strings.HasPrefix(joined, root+string(os.PathSeparator)) {
		return "", fmt.Errorf("datatransfer: refusing path escape %q", name)
	}
	return joined, nil
}

// packTar writes a tar of srcDir's contents (entries relative to srcDir) into w,
// in a deterministic (lexicographic) order. The Worker-private generation marker
// at the scratch root is excluded (issue #763); nothing else is. log is used to
// emit observability lines for vanished-file skips and cap/pad adjustments.
func packTar(srcDir string, w io.Writer, log *slog.Logger) error {
	root, err := filepath.Abs(srcDir)
	if err != nil {
		return err
	}
	info, err := os.Stat(root)
	if err != nil {
		if os.IsNotExist(err) {
			// An empty/absent working dir snapshots to an empty tar.
			return tar.NewWriter(w).Close()
		}
		return err
	}
	if !info.IsDir() {
		return fmt.Errorf("datatransfer: snapshot source %q is not a directory", srcDir)
	}

	tw := tar.NewWriter(w)
	if err := walkInto(tw, root, root, log); err != nil {
		_ = tw.Close()
		return err
	}
	return tw.Close()
}

// walkInto adds the contents of dir (relative to root) to tw, recursing in
// lexicographic order for a deterministic-ish archive.
func walkInto(tw *tar.Writer, root, dir string, log *slog.Logger) error {
	entries, err := readDir(dir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			// The directory vanished between the parent's ReadDir and this read
			// (rotated log dirs, plugin temp dirs) — the directory analog of the
			// #853 file-vanish race. Skip the subtree with a Warn rather than
			// failing the whole snapshot: by the same argument as #853 this is a
			// non-world dir (a world's region dirs cannot vanish under quiesce —
			// Minecraft never unlinks them mid-write), and a partial-loss of region
			// files is now caught downstream by the API missing-region gate (#854).
			rel, relErr := filepath.Rel(root, dir)
			if relErr != nil {
				rel = dir
			}
			log.Warn("snapshot: directory vanished between walk and read; skipping",
				"path", filepath.ToSlash(rel))
			return nil
		}
		return err
	}
	// os.ReadDir already returns entries sorted by name.
	for _, entry := range entries {
		// Exclude the Worker-private generation marker at the scratch root so it
		// never lands in the authoritative stored working set (issue #763). The match
		// is by PREFIX, not exact name (issue #834): writeGeneration writes the marker
		// atomically via a ".mcsd_generation-XXXX" temp sibling + rename, so a crash
		// before the rename leaves such a temp at the root — an exact-name exclusion
		// would let it leak into the snapshot. It only ever lives at the root, so the
		// dir == root guard keeps a same-prefixed file in a sub-tree (which would be
		// part of the legitimate world) untouched.
		if dir == root && strings.HasPrefix(entry.Name(), generationMarkerFile) {
			continue
		}
		full := filepath.Join(dir, entry.Name())
		rel, err := filepath.Rel(root, full)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)

		info, err := entryInfo(entry)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				// The entry vanished between this directory's ReadDir and the lazy
				// lstat behind Info() (e.g. log rotation, plugin temp cleanup) — the
				// remaining member of the #820/#853/#854 vanish-race family. Skip it
				// with a Warn rather than failing the whole snapshot: by the same
				// argument as those, a world's region files cannot vanish under quiesce
				// (Minecraft never unlinks them mid-write), and a partial-loss of region
				// files is caught downstream by the API missing-region gate (#854).
				log.Warn("snapshot: entry vanished between walk and stat; skipping",
					"path", rel)
				continue
			}
			return err
		}
		// Skip symlinks and other special files: a legitimate working set is plain
		// files and dirs, and following links would risk archiving outside root.
		if info.Mode()&os.ModeSymlink != 0 {
			continue
		}

		if entry.IsDir() {
			if err := tw.WriteHeader(&tar.Header{
				Name:     rel + "/",
				Typeflag: tar.TypeDir,
				Mode:     int64(info.Mode().Perm()),
			}); err != nil {
				return err
			}
			if err := walkInto(tw, root, full, log); err != nil {
				return err
			}
			continue
		}
		if !info.Mode().IsRegular() {
			continue
		}
		if err := writeRegular(tw, rel, full, info, log); err != nil {
			return err
		}
	}
	return nil
}

// writeRegular writes one regular file as a tar member.
//
// The header Size comes from the ReadDir-time stat, but the file may grow or
// shrink between that stat and the actual read (e.g. logs/latest.log written by
// a running Minecraft server even while save-off is active).
//
//   - Vanished: if the file is gone by the time we open it (ENOENT — log
//     rotation, atomic replace), it is skipped with a log line and no tar entry
//     is written. Only ENOENT on the open triggers a skip; other open errors
//     still fail the snapshot.
//   - Growth: io.LimitedReader caps the read at Size bytes, so extra bytes that
//     arrive after the header was committed are silently ignored. The cap is
//     logged so a later 422 working_set_corrupt is diagnosable.
//   - Shrink: after the LimitedReader drains the (shorter) file, the remaining
//     byte count is padded with zeros so bytes-written == header.Size (the tar
//     must be internally consistent: header size == bytes in the entry). The
//     pad delta is logged for the same reason.
func writeRegular(tw *tar.Writer, rel, full string, info os.FileInfo, log *slog.Logger) error {
	// Open before writing the header so a vanished file can be skipped cleanly
	// without leaving an uncommitted partial entry in the archive.
	f, err := openFile(full)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			// File vanished between the walk and the open (e.g. log rotation).
			// Minecraft never unlinks region files mid-write, and quiesce is
			// best-effort (RCON failure leaves the server running unbracketed),
			// so a vanished .mca would not be caught by any downstream integrity
			// gate. Warn so the event clears alerting thresholds if it occurs.
			log.Warn("snapshot: file vanished between walk and open; skipping",
				"path", rel)
			return nil
		}
		return err
	}
	defer func() { _ = f.Close() }()

	size := info.Size()
	if err := tw.WriteHeader(&tar.Header{
		Name:     rel,
		Typeflag: tar.TypeReg,
		Mode:     int64(info.Mode().Perm()),
		Size:     size,
	}); err != nil {
		return err
	}

	// Copy exactly Size bytes: a LimitedReader caps a grown file at Size so the
	// tar writer never sees more bytes than the header declared.
	lr := &io.LimitedReader{R: f, N: size}
	written, err := io.Copy(tw, lr)
	if err != nil {
		return err
	}
	if remaining := size - written; remaining > 0 {
		// File shrank between the walk stat and the copy: pad with zeros so the
		// tar entry equals header.Size (the tar must be internally consistent).
		log.Info("snapshot: file shrank between walk and copy; zero-padded",
			"path", rel, "bytes", remaining)
		if _, err := io.CopyN(tw, zeroReader{}, remaining); err != nil {
			return err
		}
	} else {
		// lr.N reaches 0 when the file had >= Size bytes: either exactly Size
		// (no adjustment) or larger (capped). Peek one byte to distinguish.
		var peek [1]byte
		if n, _ := f.Read(peek[:]); n > 0 {
			log.Info("snapshot: file grew between walk and copy; capped",
				"path", rel, "bytes_declared", size)
		}
	}
	return nil
}

// zeroReader is an infinite source of zero bytes used to pad shrunk files.
type zeroReader struct{}

func (zeroReader) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = 0
	}
	return len(p), nil
}
