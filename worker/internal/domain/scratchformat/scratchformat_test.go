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
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/regionfsck"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

const (
	persistedGenerationMarkerFile = ".mcsd_generation"
	persistedHydratePrefix        = ".hydrate-"
	// The held-set scans advertise only directories named by a server id (issue #3395).
	serverID = "7c6b5a49-3827-4165-9f4e-3d2c1b0a9f8e"
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

// A scratch tree from before the marker was authenticated (issue #3349) is still held,
// but its bare-number marker reads as generation 0: the upgrade costs each server one
// hydrate.
func TestExistingScratchTreeRemainsReadable(t *testing.T) {
	scratch := t.TempDir()
	workingDir := filepath.Join(scratch, serverID)
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(workingDir, "server.properties"), []byte("motd=existing"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(workingDir, persistedGenerationMarkerFile), []byte("17"), 0o640); err != nil {
		t.Fatal(err)
	}

	want := []session.HeldServer{{ServerID: serverID, Generation: 0}}
	key := instancemanager.LoadGenerationKey(scratch, nil)
	if got := instancemanager.ScanHeldServers(scratch, key, regionfsck.Checker{}, true, nil); !reflect.DeepEqual(got, want) {
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
	// The hydrate goes through the manager, as the Worker's does: the marker the transfer
	// adapter writes into the tree is the manager's signed rendering (issue #3349).
	key := instancemanager.LoadGenerationKey(scratch, nil)
	m := instancemanager.New(nil, scratch, regionfsck.Checker{}, nil).
		WithGenerationKey(key).
		WithTransfer(datatransfer.New(srv.Client()))
	t.Cleanup(m.Close)
	res := m.Handle(context.Background(), session.Command{
		CommandID: "h1", ServerID: serverID, Kind: "HydrateTrigger", TransferURL: srv.URL, TransferToken: "tok",
	})
	if !res.Success || res.HeldGeneration == nil || *res.HeldGeneration != servedGeneration {
		t.Fatalf("hydrate = %+v, want success declaring %d", res, servedGeneration)
	}

	wantHeld := []session.HeldServer{{ServerID: serverID, Generation: servedGeneration}}
	if got := instancemanager.ScanHeldServers(scratch, key, regionfsck.Checker{}, true, nil); !reflect.DeepEqual(got, wantHeld) {
		t.Fatalf("held after Hydrate = %v, want %v", got, wantHeld)
	}

	leftovers := []string{
		filepath.Join(scratch, persistedHydratePrefix+serverID+"-123456"),
		filepath.Join(scratch, persistedHydratePrefix+serverID+"-superseded-654321"),
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

	if got := instancemanager.ScanHeldServers(scratch, key, regionfsck.Checker{}, true, nil); !reflect.DeepEqual(got, wantHeld) {
		t.Fatalf("held with crash-left hydrate trees = %v, want %v", got, wantHeld)
	}
	if got := m.HeldServers(); !reflect.DeepEqual(got, wantHeld) {
		t.Fatalf("manager held with crash-left hydrate trees = %v, want %v", got, wantHeld)
	}

	instancemanager.ReclaimHydrateLeftovers(scratch)
	for _, dir := range leftovers {
		if _, err := os.Stat(dir); !os.IsNotExist(err) {
			t.Fatalf("crash-left hydrate tree %q remains: %v", dir, err)
		}
	}
	if got := instancemanager.ScanHeldServers(scratch, key, regionfsck.Checker{}, true, nil); !reflect.DeepEqual(got, wantHeld) {
		t.Fatalf("held after reclaim = %v, want %v", got, wantHeld)
	}
}
