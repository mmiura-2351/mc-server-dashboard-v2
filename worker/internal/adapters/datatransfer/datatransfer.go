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
)

// generationMarkerFile is the Worker-private marker the instance manager writes
// at the scratch root to record its local working-set generation (issue #763).
// It is excluded from a snapshot pack so this Worker-private state never lands in
// the authoritative stored working set (and is never re-hydrated to another
// Worker or the live Minecraft dir); Hydrate re-writes it fresh from the response
// header after unpack, so excluding it on pack is purely corrective. Kept as a
// local constant to avoid the adapter depending on the instancemanager package.
const generationMarkerFile = ".mcsd_generation"

// generationHeader is the response header the API data plane stamps on a hydrate
// (the store generation served) and a snapshot (the new store generation
// published) so the Worker can record the generation of its local working set
// (issue #763). An absent or unparseable header is read as generation 0.
const generationHeader = "X-Working-Set-Generation"

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
// adjustments and vanished-file skips). The default is slog.Default().
func (c *Client) WithLogger(l *slog.Logger) *Client {
	c.logger = l
	return c
}

// Hydrate downloads the working-set tar from url into destDir, REPLACING its
// contents wholesale: the tar is unpacked into a fresh temp sibling that is then
// atomically swapped into destDir, so a retained stale working set is replaced
// (not merged) and any symlink a previous run planted in destDir is never
// traversed (issue #772). The swap discards the old destDir entirely, including
// the Worker-private generation marker; the caller rewrites that marker fresh
// from the served generation after Hydrate returns (issue #763). A 204 response
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

	if err := unpackAndSwap(resp.Body, destDir); err != nil {
		return 0, fmt.Errorf("datatransfer: unpack: %w", err)
	}
	// The store generation the API served, recorded by the caller alongside the
	// freshly unpacked working set (issue #763).
	return parseGeneration(resp.Header), nil
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

// Snapshot packs srcDir into a tar and uploads it to url. The tar is spooled to a
// temp file (in srcDir's parent, i.e. the scratch root, so it shares srcDir's
// filesystem) rather than a memory buffer: a multi-GB world times the bounded
// concurrent transfers would otherwise pin gigabytes of RAM. The file is Stat'd
// for the Content-Length the API's proven-complete gate matches, streamed as the
// request body, and removed on every path (delta/streamed snapshot is deferred,
// FR-DATA-5). A crash before that deferred remove leaks the spool; SweepSnapshotSpools
// reclaims such leftovers at startup (issue #787).
func (c *Client) Snapshot(ctx context.Context, url, token, srcDir string) (uint64, error) {
	spool, err := os.CreateTemp(filepath.Dir(srcDir), snapshotSpoolPrefix+"*.tar")
	if err != nil {
		return 0, fmt.Errorf("datatransfer: create snapshot spool: %w", err)
	}
	defer func() {
		_ = spool.Close()
		_ = os.Remove(spool.Name())
	}()

	if err := packTar(srcDir, spool, c.logger); err != nil {
		return 0, fmt.Errorf("datatransfer: pack: %w", err)
	}
	size, err := spool.Seek(0, io.SeekCurrent)
	if err != nil {
		return 0, fmt.Errorf("datatransfer: size snapshot spool: %w", err)
	}
	if _, err := spool.Seek(0, io.SeekStart); err != nil {
		return 0, fmt.Errorf("datatransfer: rewind snapshot spool: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, spool)
	if err != nil {
		return 0, fmt.Errorf("datatransfer: build snapshot request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/x-tar")
	// Set an explicit length so the API's proven-complete gate can match it.
	req.ContentLength = size

	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("datatransfer: snapshot request: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("datatransfer: snapshot: unexpected status %s", resp.Status)
	}
	// The new store generation the publish produced, recorded by the caller as the
	// generation its scratch is now at (issue #763).
	return parseGeneration(resp.Header), nil
}

// unpackAndSwap unpacks the tar stream into a fresh temp sibling of destDir, then
// atomically swaps it into place (issue #772). Unpacking into a brand-new tree —
// rather than over the retained scratch — gives REPLACE semantics (files deleted
// upstream do not survive) and means a symlink a previous run planted in destDir
// is never traversed (the destination tree has no pre-existing entries). The
// generation marker is intentionally NOT carried across the swap: the caller
// rewrites it fresh from the served generation after Hydrate returns (issue #763).
//
// The temp and trash dirs are dot-prefixed siblings in destDir's parent (the
// scratch root). ScanHeldServers (scratchscan.go) reports every scratch subdir as
// a held server, but the API only consults that list for ids it assigned, so a
// crash-leftover .hydrate-* sibling is never matched; a stale one is also reclaimed
// by the next hydrate's leftover sweep below (if the id is re-placed here) and by
// the post-final-snapshot scratch GC, which sweeps this id's .hydrate-<id>-* siblings
// alongside removing scratchDir/<id> once the stopped-id final snapshot publishes
// (issue #766/#841/#842, instancemanager.removeScratch).
//
// Crash safety: the temp tree is built fully before any rename. The swap then does
// (1) rename destDir -> trash, (2) rename temp -> destDir, (3) remove trash. A
// crash between (1) and (2) leaves destDir absent but BOTH the trash (old) and temp
// (new) copies on disk, so no data is lost; the next start re-hydrates regardless
// (the missing destDir reports as "holding nothing") and the leftover sweep clears
// the orphans. A crash after (2) leaves a stale trash dir, swept next time.
func unpackAndSwap(r io.Reader, destDir string) error {
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

	// Durability ordering (issue #787): make the fully built temp tree durable
	// BEFORE the swap renames. unpackTar already fsynced each file's contents; this
	// fsyncs every directory in the tree so the dir entries (the names pointing at
	// those files) are durable too. A power loss after the swap must never persist
	// the new destDir (and the generation marker the caller then writes) over a tree
	// whose files or names are not yet on disk — the #767 skip gate would boot that
	// torn world.
	if err := fsyncTree(tmpDir); err != nil {
		return err
	}

	trashDir := tmpDir + ".trash"
	swapped := false
	if err := os.Rename(destDir, trashDir); err != nil {
		if !os.IsNotExist(err) {
			return err
		}
		// No prior working set to displace; the destDir slot is free.
	} else {
		swapped = true
	}
	if err := swapRename(tmpDir, destDir); err != nil {
		if swapped {
			// Restore the old working set so the failure does not lose both copies.
			_ = os.Rename(trashDir, destDir)
		}
		return err
	}
	// fsync the scratch root so the swap renames themselves are durable: the marker
	// the caller writes next (writeGeneration, also fsynced) can then never become
	// durable before the destDir tree it describes (issue #787).
	if err := fsyncDir(parent); err != nil {
		return err
	}
	if swapped {
		_ = os.RemoveAll(trashDir)
	}
	return nil
}

// swapRename is the final temp->destDir swap rename, indirected through a package
// var so a test can force it to fail and exercise the trash-restore path (the swap
// renames within one parent dir are symmetric, so there is no static-perms way to
// fail only this one). Production always uses os.Rename.
var swapRename = os.Rename

// openFile is the function used by writeRegular to open a file for reading. It
// is indirected through a package var so a test can inject ENOENT for a specific
// path (simulating log-rotation deletion between the walk and the open) without
// needing to race real filesystem timings. Production always uses os.Open.
var openFile = os.Open

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
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	// os.ReadDir already returns entries sorted by name.
	for _, entry := range entries {
		// Exclude the Worker-private generation marker at the scratch root so it
		// never lands in the authoritative stored working set (issue #763). It only
		// ever lives at the root, so the dir == root guard keeps a same-named file in
		// a sub-tree (which would be part of the legitimate world) untouched.
		if dir == root && entry.Name() == generationMarkerFile {
			continue
		}
		full := filepath.Join(dir, entry.Name())
		rel, err := filepath.Rel(root, full)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)

		info, err := entry.Info()
		if err != nil {
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
			// By definition this is not a quiesced or world file; skip it.
			log.Info("snapshot: file vanished between walk and open; skipping",
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
