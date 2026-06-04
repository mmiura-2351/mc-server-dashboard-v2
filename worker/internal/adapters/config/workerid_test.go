package config

import (
	"os"
	"path/filepath"
	"testing"
)

// baseEnv returns a minimal valid env with scratch_dir pointing at dir and no
// explicit worker.id, so worker id resolution is exercised.
func baseEnv(dir string) map[string]string {
	return map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":  "api:50051",
		"MCD_WORKER_API_DATA_PLANE_URL": "https://api/data",
		"MCD_WORKER_API_CREDENTIAL":     "secret",
		"MCD_WORKER_API_TLS_INSECURE":   "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR": dir,
	}
}

func TestLoadGeneratesAndPersistsWorkerID(t *testing.T) {
	dir := t.TempDir()

	cfg, err := Load("", mapEnv(baseEnv(dir)))
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if !isUUID(cfg.Worker.ID) {
		t.Fatalf("Worker.ID = %q, want a generated UUID", cfg.Worker.ID)
	}

	path := filepath.Join(dir, workerIDFileName)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("worker id not persisted at %q: %v", path, err)
	}
	if got := string(data); got != cfg.Worker.ID+"\n" {
		t.Errorf("persisted file = %q, want %q", got, cfg.Worker.ID+"\n")
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("worker id file perm = %o, want 0600", perm)
	}
}

func TestLoadReusesPersistedWorkerID(t *testing.T) {
	dir := t.TempDir()

	first, err := Load("", mapEnv(baseEnv(dir)))
	if err != nil {
		t.Fatalf("first Load() error = %v", err)
	}

	second, err := Load("", mapEnv(baseEnv(dir)))
	if err != nil {
		t.Fatalf("second Load() error = %v", err)
	}

	if first.Worker.ID != second.Worker.ID {
		t.Errorf("worker id not stable across loads: %q != %q", first.Worker.ID, second.Worker.ID)
	}
}

func TestLoadUsesExplicitUUIDAsIs(t *testing.T) {
	dir := t.TempDir()
	const explicit = "123e4567-e89b-12d3-a456-426614174000"

	env := baseEnv(dir)
	env["MCD_WORKER_WORKER_ID"] = explicit

	cfg, err := Load("", mapEnv(env))
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.Worker.ID != explicit {
		t.Errorf("Worker.ID = %q, want explicit %q", cfg.Worker.ID, explicit)
	}

	// An explicit id must not be persisted: the file is the zero-config path only.
	if _, err := os.Stat(filepath.Join(dir, workerIDFileName)); !os.IsNotExist(err) {
		t.Errorf("worker-id file should not be written for an explicit id (stat err = %v)", err)
	}
}

func TestLoadRejectsExplicitNonUUID(t *testing.T) {
	dir := t.TempDir()

	env := baseEnv(dir)
	env["MCD_WORKER_WORKER_ID"] = "worker-host-01"

	_, err := Load("", mapEnv(env))
	if err == nil {
		t.Fatal("Load() with non-UUID worker.id: want error, got nil")
	}
	if !contains(err.Error(), "worker.id") || !contains(err.Error(), "UUID") {
		t.Errorf("error %q does not explain the UUID requirement", err.Error())
	}
}

func TestLoadFailsOnUnreadablePersistedWorkerID(t *testing.T) {
	dir := t.TempDir()
	// A malformed persisted file is a fatal error, not a silent regeneration.
	path := filepath.Join(dir, workerIDFileName)
	if err := os.WriteFile(path, []byte("not-a-uuid"), 0o600); err != nil {
		t.Fatal(err)
	}

	_, err := Load("", mapEnv(baseEnv(dir)))
	if err == nil {
		t.Fatal("Load() with malformed persisted worker id: want error, got nil")
	}
	if !contains(err.Error(), "not a UUID") {
		t.Errorf("error %q does not explain the malformed persisted id", err.Error())
	}
}

func TestLoadFailsWhenWorkerIDFileUnreadable(t *testing.T) {
	dir := t.TempDir()
	// Make <scratch_dir>/worker-id a directory so ReadFile fails with a
	// non-NotExist error: that must surface as a clear error, not be swallowed.
	if err := os.Mkdir(filepath.Join(dir, workerIDFileName), 0o700); err != nil {
		t.Fatal(err)
	}

	_, err := Load("", mapEnv(baseEnv(dir)))
	if err == nil {
		t.Fatal("Load() with unreadable worker id file: want error, got nil")
	}
	if !contains(err.Error(), "worker id file") {
		t.Errorf("error %q does not name the worker id file", err.Error())
	}
}

func TestNewUUIDv4Format(t *testing.T) {
	id, err := newUUIDv4()
	if err != nil {
		t.Fatalf("newUUIDv4() error = %v", err)
	}
	if !isUUID(id) {
		t.Fatalf("newUUIDv4() = %q, not a canonical UUID", id)
	}
	// Version nibble is 4 and variant nibble is 8/9/a/b.
	if id[14] != '4' {
		t.Errorf("version nibble = %c, want 4 (id %q)", id[14], id)
	}
	switch id[19] {
	case '8', '9', 'a', 'b':
	default:
		t.Errorf("variant nibble = %c, want 8/9/a/b (id %q)", id[19], id)
	}
}
