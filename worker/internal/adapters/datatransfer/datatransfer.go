// Package datatransfer moves working sets over HTTP using bearer tokens and an injected TLS policy.
// Hydrate replaces scratch from a sanitized tar; snapshots spool to disk for bounded memory and Content-Length.
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

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
)

// generationHeader is the response header the API data plane stamps on a hydrate (the store generation served)
// and a snapshot (the new store generation published) so the Worker can record the generation of its local
// working set. An absent or unparseable header is read as generation 0.
const generationHeader = "X-Working-Set-Generation"

// Omit base-generation zero; otherwise the API compares it with the current generation when deciding staleness.
const baseGenerationHeader = "X-Working-Set-Base-Generation"

// Worker identity permits same-Worker republish after response loss; unknown publishers keep the permissive
// fallback.
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

// Hydrate swaps a validated 200 tar and its generation marker into destDir, preserving displaced recovery data.
// A 204 returns the generation without touching destDir; the caller records its marker.
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
		// A 204 leaves destDir untouched; the API must skip hydrate when generation-zero storage meets held scratch.
		// Any future mutation must replace the directory so running-snapshot identity guards can detect it.
		return parseGeneration(resp.Header), nil
	case http.StatusOK:
	default:
		return 0, fmt.Errorf("datatransfer: hydrate: unexpected status %s", resp.Status)
	}

	gen := parseGeneration(resp.Header)
	if err := unpackAndSwap(resp.Body, destDir, gen, c.logger); err != nil {
		return 0, fmt.Errorf("datatransfer: unpack: %w", err)
	}
	// The store generation the API served, recorded by the caller alongside the freshly unpacked working set. The
	// marker was already written into the temp tree before the swap-in rename, so this return value is still used
	// by the caller's recordGeneration for the 204 path and is idempotent on the 200 path.
	return gen, nil
}

// snapshotSpoolPrefix is the temp-file prefix Snapshot uses for its tar spool in
// the scratch root. SweepSnapshotSpools matches it at startup to reclaim spools a
// crash mid-snapshot left behind.
const snapshotSpoolPrefix = "snapshot-"

// SweepSnapshotSpools removes only top-level snapshot-*.tar files left by crashes.
// Run before transfers start; directory scans cannot reclaim them.
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

// PackSnapshot spools to the scratch filesystem and returns mandatory cleanup.
// Only packing reads live scratch; upload may run after save-on, and boot reclaim covers crash-left spools.
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

// UploadSnapshot streams the tar spool file at spoolPath to url, declaring baseGeneration and workerID for the
// API's publish-time generation guard. It returns the NEW store generation the publish produced (the value of
// the API's response header); 0 when the header is absent (an older API).
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

// unpackAndSwap builds and fsyncs a fresh tree with its marker, parks the old tree, then swaps in the new one.
// Keep an occupied recovery slot; on swap failure restore the parked tree, and never delete it before successful
// replacement.
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

	// Publish the marker with the replacement tree, never after swap-in.
	// Fsync its contents and directory entry before rename so a crash cannot separate the claimed generation from
	// its data.
	if err := writeFile(filepath.Join(tmpDir, scratchformat.GenerationMarkerFile),
		strings.NewReader(strconv.FormatUint(gen, 10)), 0o640); err != nil {
		return err
	}

	// Fsync temp-tree directory entries after file contents and before swap so the durable marker cannot outlive
	// its data.
	if err := fsyncTree(tmpDir); err != nil {
		return err
	}

	// Park the old tree before swap-in so failure can restore it.
	// Budget for three trees when the displaced slot is occupied: retained recovery, parked live data, and unpacked
	// replacement.
	displaced := displacedDir(destDir)
	asideAt := ""      // where the live destDir was parked; empty when there was nothing to displace
	dropAside := false // true when asideAt is the sweepable name, i.e. an older displaced tree is being kept
	if _, err := os.Lstat(destDir); err == nil {
		// Oldest recovery wins: retain an occupied slot and drop the newly displaced branch only after swap-in.
		// That branch may contain newer unpublished play; warn about the loss rather than selecting by health or age.
		info, held, slotErr := displacedSlotHoldsWorkingSet(displaced)
		if slotErr != nil {
			return slotErr
		}
		if held {
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
			// The slot is free (or held only junk, already cleared): take the ordinary displace path, which parks the
			// live set DIRECTLY at.displaced-<id>, never under an intermediate name a later sweep would delete.
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
			// If swap-in and restore both fail, asideAt still retains the old tree and any prior displaced copy is
			// untouched.
			_ = os.Rename(asideAt, destDir)
		}
		return err
	}
	// Before dropping the superseded branch, recheck that the retained recovery slot still exists.
	// If a concurrent sweep emptied it, re-park the branch there instead; uncertain failures leave a sweepable
	// leftover.
	if dropAside {
		if _, err := os.Lstat(displaced); err == nil {
			_ = os.RemoveAll(asideAt)
		} else if os.IsNotExist(err) && os.Rename(asideAt, displaced) == nil {
			log.Info("hydrate: the retained displaced tree was swept by a concurrent snapshot before the discard; keeping the replaced working set in its slot instead (issue #3112)",
				"server_id", filepath.Base(destDir),
				"retained", displaced)
		}
	}
	// fsync the scratch root so BOTH swap renames (the displace-aside and the swap-in), and a re-park above, are
	// durable: a power loss must not roll the displace rename back, and the marker the caller writes next
	// (writeGeneration, also fsynced) can then never become durable before the destDir tree it describes.
	if err := fsyncDir(parent); err != nil {
		return err
	}
	return nil
}

// Retain real working-set directories; clear files, symlinks, and empty or marker-only directories.
// Read errors fail hydrate without deleting data; the displaced sweep must rename before traversing the slot.
func displacedSlotHoldsWorkingSet(displaced string) (os.FileInfo, bool, error) {
	info, err := os.Lstat(displaced)
	if os.IsNotExist(err) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	if info.IsDir() {
		entries, readErr := readDir(displaced)
		if readErr != nil {
			return nil, false, readErr
		}
		for _, e := range entries {
			if !strings.HasPrefix(e.Name(), scratchformat.GenerationMarkerFile) {
				return info, true, nil
			}
		}
	}
	_ = os.RemoveAll(displaced)
	return nil, false, nil
}

// displacedDir is the per-server path the swap moves a displaced old working set to: a dot-prefixed sibling of
// destDir so it cannot collide with a server-id scratch dir and is never matched to an assigned id by the API.
// One per server (no random suffix): the name is written only when the slot is free, so exactly one displaced
// tree per server exists at any time.
func displacedDir(destDir string) string {
	return filepath.Join(filepath.Dir(destDir), displacedPrefix+filepath.Base(destDir))
}

// displacedPrefix is the dot-prefixed name prefix for a displaced old working set, kept aside by a hydrate and
// GC'd on the next successful snapshot for the server (instancemanager.sweepDisplaced).
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

// Inject directory disappearance and recovery-slot read errors deterministically; chmod fixtures do not fail for
// root.
var readDir = os.ReadDir

// entryInfo resolves a DirEntry's FileInfo for walkInto. os.DirEntry.Info lazily lstats the entry, so it returns
// ENOENT when the entry vanishes between the ReadDir walk and this call, the same race family as
// openFile/readDir. Indirected through a package var so a test can inject ENOENT for a specific entry without
// racing real filesystem timings. Production always uses entry.Info.
var entryInfo = func(entry os.DirEntry) (os.FileInfo, error) { return entry.Info() }

// fsyncTree flushes directories child-first; writeFile has already flushed file contents.
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
	return scratchformat.HydratePrefix + filepath.Base(destDir) + "-"
}

// sweepHydrateLeftovers removes temp/trash dirs a previous crashed hydrate for the
// same server left in parent. It is best-effort: a removal failure is ignored (the
// stale dir is harmless — ScanHeldServers never matches it to an assigned id).
func sweepHydrateLeftovers(parent, serverID string) {
	entries, err := os.ReadDir(parent)
	if err != nil {
		return
	}
	prefix := scratchformat.HydratePrefix + serverID + "-"
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

// Fsync file contents before swap; rename alone cannot make the data durable.
// Flush directory entries after unpack.
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

// packTar writes a tar of srcDir's contents (entries relative to srcDir) into w, in a deterministic
// (lexicographic) order. The Worker-private generation marker at the scratch root is excluded; nothing else is.
// log is used to emit observability lines for vanished-file skips and cap/pad adjustments.
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
			// Skip vanished subtrees with a warning; the API missing-region gate remains the backstop for world-data
			// loss.
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
		// Exclude root-level marker files and temp siblings by prefix; same-prefixed files below root remain world
		// content.
		if dir == root && strings.HasPrefix(entry.Name(), scratchformat.GenerationMarkerFile) {
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
				// Skip entries that vanish before Info with a warning; other errors still abort packing.
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

// Keep tar entries consistent when files change after stat: cap growth, zero-pad shrinkage, and skip vanished
// files.
// Only ENOENT on open skips; other I/O failures abort the pack.
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
