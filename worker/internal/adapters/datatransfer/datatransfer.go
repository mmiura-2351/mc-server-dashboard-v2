// Package datatransfer is the Worker's HTTP data-plane client: it moves a
// server's working set between the API's authoritative Storage and the local
// scratch dir (FR-DATA-3, FR-DATA-4). The control plane only triggers a
// transfer and hands over a URL + token (CONTROL_PLANE.md Section 5.2); this
// adapter does the bulk byte movement, off the gRPC stream.
//
//   - Hydrate: GET the working-set tar and stream-unpack it into the instance
//     working dir. Members are path-sanitized (absolute paths and "..", and any
//     symlink/hardlink escape, are rejected), mirroring the API-side filter="data"
//     discipline so a hostile archive cannot escape the working dir. A 204 No
//     Content means the server has no published working set yet; the Worker treats
//     it as an empty dir and launches fresh.
//   - Snapshot: pack the working dir into a tar and POST it with a Content-Length
//     so the API's "proven complete" gate can verify the streamed byte count
//     (STORAGE.md Section 4.1, FR-DATA-6).
//
// Transport security mirrors the control channel (CONFIGURATION.md Section 6.1):
// the same CA bundle / mTLS / insecure-dev posture is reused via the injected
// *http.Client built in the wiring layer. The transfer token travels as
// "Authorization: Bearer <token>", the same credential model as the stream.
package datatransfer

import (
	"archive/tar"
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
)

// Client moves working sets over the API HTTP data plane. It is safe for
// concurrent use (it holds only an *http.Client).
type Client struct {
	http *http.Client
}

// New builds a Client over the given *http.Client (built with the control
// channel's TLS posture in the wiring layer).
func New(httpClient *http.Client) *Client {
	return &Client{http: httpClient}
}

// Hydrate downloads the working-set tar from url into destDir, replacing its
// contents. A 204 response means "no published working set"; destDir is left
// empty and Hydrate returns nil (the Worker launches against an empty dir). Any
// archive member that would escape destDir is rejected and aborts the transfer.
func (c *Client) Hydrate(ctx context.Context, url, token, destDir string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("datatransfer: build hydrate request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("datatransfer: hydrate request: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	switch resp.StatusCode {
	case http.StatusNoContent:
		// No published working set yet; nothing to unpack.
		return nil
	case http.StatusOK:
	default:
		return fmt.Errorf("datatransfer: hydrate: unexpected status %s", resp.Status)
	}

	if err := os.MkdirAll(destDir, 0o750); err != nil {
		return fmt.Errorf("datatransfer: prepare working dir: %w", err)
	}
	if err := unpackTar(resp.Body, destDir); err != nil {
		return fmt.Errorf("datatransfer: unpack: %w", err)
	}
	return nil
}

// Snapshot packs srcDir into a tar and uploads it to url. The tar is buffered to
// compute a Content-Length so the API can verify the transfer is complete; a
// Minecraft working set is small enough that a memory buffer is acceptable at M1
// (delta/streamed snapshot is deferred, FR-DATA-5).
func (c *Client) Snapshot(ctx context.Context, url, token, srcDir string) error {
	var buf bytes.Buffer
	if err := packTar(srcDir, &buf); err != nil {
		return fmt.Errorf("datatransfer: pack: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, &buf)
	if err != nil {
		return fmt.Errorf("datatransfer: build snapshot request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/x-tar")
	// Set an explicit length so the API's proven-complete gate can match it.
	req.ContentLength = int64(buf.Len())

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("datatransfer: snapshot request: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusOK {
		return fmt.Errorf("datatransfer: snapshot: unexpected status %s", resp.Status)
	}
	return nil
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

// writeFile creates target and copies the member body into it.
func writeFile(target string, src io.Reader, mode os.FileMode) error {
	if mode == 0 {
		mode = 0o640
	}
	out, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode.Perm())
	if err != nil {
		return err
	}
	defer func() { _ = out.Close() }()
	if _, err := io.Copy(out, src); err != nil {
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
// in a deterministic (lexicographic) order. Nothing is excluded at M1.
func packTar(srcDir string, w io.Writer) error {
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
	if err := walkInto(tw, root, root); err != nil {
		_ = tw.Close()
		return err
	}
	return tw.Close()
}

// walkInto adds the contents of dir (relative to root) to tw, recursing in
// lexicographic order for a deterministic-ish archive.
func walkInto(tw *tar.Writer, root, dir string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	// os.ReadDir already returns entries sorted by name.
	for _, entry := range entries {
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
			if err := walkInto(tw, root, full); err != nil {
				return err
			}
			continue
		}
		if !info.Mode().IsRegular() {
			continue
		}
		if err := writeRegular(tw, rel, full, info); err != nil {
			return err
		}
	}
	return nil
}

// writeRegular writes one regular file as a tar member.
func writeRegular(tw *tar.Writer, rel, full string, info os.FileInfo) error {
	if err := tw.WriteHeader(&tar.Header{
		Name:     rel,
		Typeflag: tar.TypeReg,
		Mode:     int64(info.Mode().Perm()),
		Size:     info.Size(),
	}); err != nil {
		return err
	}
	f, err := os.Open(full)
	if err != nil {
		return err
	}
	defer func() { _ = f.Close() }()
	if _, err := io.Copy(tw, f); err != nil {
		return err
	}
	return nil
}
