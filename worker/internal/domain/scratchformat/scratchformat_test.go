package scratchformat_test

import (
	"archive/tar"
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/datatransfer"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

const (
	persistedGenerationMarkerFile = ".mcsd_generation"
	persistedHydratePrefix        = ".hydrate-"
)

// These names are persisted in existing Worker scratch trees. Keep this oracle
// independent of the production constants so a coordinated rename still fails.
func TestPersistedScratchNames(t *testing.T) {
	if got := scratchformat.GenerationMarkerFile; got != persistedGenerationMarkerFile {
		t.Errorf("generation marker = %q, want the persisted filename", got)
	}
	if got := scratchformat.HydratePrefix; got != persistedHydratePrefix {
		t.Errorf("hydrate prefix = %q, want the persisted prefix", got)
	}
}

func TestExistingScratchTreeRemainsReadable(t *testing.T) {
	scratch := t.TempDir()
	workingDir := filepath.Join(scratch, "server")
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(workingDir, "server.properties"), []byte("motd=existing"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(workingDir, persistedGenerationMarkerFile), []byte("17"), 0o640); err != nil {
		t.Fatal(err)
	}

	want := []session.HeldServer{{ServerID: "server", Generation: 17}}
	if got := instancemanager.ScanHeldServers(scratch, true, nil); !reflect.DeepEqual(got, want) {
		t.Fatalf("existing scratch held = %v, want %v", got, want)
	}
}

func TestHydrateScratchIsDiscoveredAndCrashLeftTreesAreReclaimed(t *testing.T) {
	const servedGeneration = 42
	var body bytes.Buffer
	tw := tar.NewWriter(&body)
	content := []byte("motd=hydrated")
	if err := tw.WriteHeader(&tar.Header{Name: "server.properties", Mode: 0o640, Size: int64(len(content))}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(content); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("X-Working-Set-Generation", "42")
		_, _ = w.Write(body.Bytes())
	}))
	defer srv.Close()

	scratch := t.TempDir()
	workingDir := filepath.Join(scratch, "server")
	gen, err := datatransfer.New(srv.Client()).Hydrate(context.Background(), srv.URL, "tok", workingDir)
	if err != nil {
		t.Fatalf("Hydrate: %v", err)
	}
	if gen != servedGeneration {
		t.Fatalf("Hydrate generation = %d, want %d", gen, servedGeneration)
	}

	wantHeld := []session.HeldServer{{ServerID: "server", Generation: servedGeneration}}
	if got := instancemanager.ScanHeldServers(scratch, true, nil); !reflect.DeepEqual(got, wantHeld) {
		t.Fatalf("held after Hydrate = %v, want %v", got, wantHeld)
	}

	leftovers := []string{
		filepath.Join(scratch, persistedHydratePrefix+"server-123456"),
		filepath.Join(scratch, persistedHydratePrefix+"server-superseded-654321"),
	}
	for _, dir := range leftovers {
		if err := os.MkdirAll(dir, 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "server.properties"), content, 0o640); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, persistedGenerationMarkerFile), []byte("42"), 0o640); err != nil {
			t.Fatal(err)
		}
	}

	if got := instancemanager.ScanHeldServers(scratch, true, nil); !reflect.DeepEqual(got, wantHeld) {
		t.Fatalf("held with crash-left hydrate trees = %v, want %v", got, wantHeld)
	}
	m := instancemanager.New(nil, scratch, nil)
	t.Cleanup(m.Close)
	if got := m.HeldServers(); !reflect.DeepEqual(got, wantHeld) {
		t.Fatalf("manager held with crash-left hydrate trees = %v, want %v", got, wantHeld)
	}

	instancemanager.ReclaimHydrateLeftovers(scratch)
	for _, dir := range leftovers {
		if _, err := os.Stat(dir); !os.IsNotExist(err) {
			t.Fatalf("crash-left hydrate tree %q remains: %v", dir, err)
		}
	}
	if got := instancemanager.ScanHeldServers(scratch, true, nil); !reflect.DeepEqual(got, wantHeld) {
		t.Fatalf("held after reclaim = %v, want %v", got, wantHeld)
	}
}
