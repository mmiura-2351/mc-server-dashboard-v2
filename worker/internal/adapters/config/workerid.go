package config

// Worker identity defaulting and validation. The API rejects a non-UUID
// worker id at registration (issue #99), because a server's assigned worker is
// a UUID column. So an explicit worker.id must be a UUID, and the zero-config
// default must also be a UUID. To keep identity stable across restarts (so the
// API's assignment rebuild and assigned_worker_id rows stay coherent), the
// default is a UUIDv4 persisted at <scratch_dir>/worker-id: generated once on
// first boot, then read back on every later load.

import (
	"crypto/rand"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// workerIDFileName is the file under scratch_dir that holds the persisted
// zero-config worker id.
const workerIDFileName = "worker-id"

// resolveWorkerID fills cfg.Worker.ID when unset and validates it. An explicit
// id (from file or env) must be a UUID and is used as-is; a non-UUID explicit
// id fails fast client-side (better operator UX than a remote gRPC abort). When
// unset, the id comes from <scratch_dir>/worker-id, generated and persisted on
// first boot so it stays stable across restarts.
func resolveWorkerID(cfg *Config) error {
	if cfg.Worker.ID != "" {
		if !isUUID(cfg.Worker.ID) {
			return fmt.Errorf("config: worker.id %q is not a UUID (the API rejects non-UUID worker ids at registration)", cfg.Worker.ID)
		}
		return nil
	}

	id, err := persistedWorkerID(cfg.Worker.ScratchDir)
	if err != nil {
		return err
	}
	cfg.Worker.ID = id
	return nil
}

// persistedWorkerID returns the worker id stored at <scratchDir>/worker-id,
// generating and writing a new UUIDv4 on first boot. A present-but-unreadable
// or malformed file is a fatal error, not a silent regeneration: a changing id
// would orphan the API's assignment rows.
func persistedWorkerID(scratchDir string) (string, error) {
	path := filepath.Join(scratchDir, workerIDFileName)

	data, err := os.ReadFile(path)
	switch {
	case err == nil:
		id := strings.TrimSpace(string(data))
		if !isUUID(id) {
			return "", fmt.Errorf("config: persisted worker id %q at %q is not a UUID; remove the file to regenerate", id, path)
		}
		return id, nil
	case errors.Is(err, fs.ErrNotExist):
		// First boot: generate and persist below.
	default:
		return "", fmt.Errorf("config: read worker id file %q: %w", path, err)
	}

	id, err := newUUIDv4()
	if err != nil {
		return "", fmt.Errorf("config: generate worker id: %w", err)
	}
	if err := writeWorkerIDFile(path, id); err != nil {
		return "", err
	}
	return id, nil
}

// writeWorkerIDFile atomically writes id to path with 0600 permissions via a
// temp file in the same directory and a rename.
func writeWorkerIDFile(path, id string) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, workerIDFileName+".*")
	if err != nil {
		return fmt.Errorf("config: create temp worker id file in %q: %w", dir, err)
	}
	tmpPath := tmp.Name()
	defer func() { _ = os.Remove(tmpPath) }()

	if _, err := tmp.WriteString(id + "\n"); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("config: write worker id file: %w", err)
	}
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("config: chmod worker id file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("config: close worker id file: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return fmt.Errorf("config: persist worker id file %q: %w", path, err)
	}
	return nil
}

// newUUIDv4 returns a random RFC 4122 version-4 UUID in the canonical
// 8-4-4-4-12 lower-case hex form. It hand-rolls the formatting to avoid a new
// dependency.
func newUUIDv4() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant 10
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}

// isUUID reports whether s is a canonical 8-4-4-4-12 hex UUID. It mirrors what
// the API accepts (Python's uuid.UUID), which is hyphen-grouped hex of any
// version; case is allowed on either side.
func isUUID(s string) bool {
	if len(s) != 36 {
		return false
	}
	for i, c := range s {
		switch i {
		case 8, 13, 18, 23:
			if c != '-' {
				return false
			}
		default:
			if !isHex(c) {
				return false
			}
		}
	}
	return true
}

func isHex(c rune) bool {
	return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
}
