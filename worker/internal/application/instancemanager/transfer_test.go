package instancemanager

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// fakeTransfer records hydrate/snapshot calls and returns a canned error.
type fakeTransfer struct {
	mu        sync.Mutex
	hydrated  []string // workingDir args
	snapshots []string
	err       error
}

func (f *fakeTransfer) Hydrate(_ context.Context, _, _, workingDir string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.hydrated = append(f.hydrated, workingDir)
	return f.err
}

func (f *fakeTransfer) Snapshot(_ context.Context, _, _, workingDir string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.snapshots = append(f.snapshots, workingDir)
	return f.err
}

func hydrateCmd() session.Command {
	return session.Command{
		CommandID: "h1", ServerID: "s1", Kind: "HydrateTrigger",
		TransferURL: "https://api/working-set", TransferToken: "tok",
	}
}

func snapshotCmd() session.Command {
	return session.Command{
		CommandID: "p1", ServerID: "s1", Kind: "SnapshotTrigger",
		TransferURL: "https://api/snapshot", TransferToken: "tok",
	}
}

func TestHydrateTriggerPullsIntoWorkingDir(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	res := m.Handle(context.Background(), hydrateCmd())
	if !res.Success {
		t.Fatalf("HydrateTrigger result = %+v, want success", res)
	}
	want := filepath.Join(m.scratchDir, "s1")
	if len(tr.hydrated) != 1 || tr.hydrated[0] != want {
		t.Fatalf("hydrated = %v, want [%q]", tr.hydrated, want)
	}
}

func TestHydrateTriggerOnRunningServerIsInvalidState(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start failed: %+v", res)
	}
	res := m.Handle(context.Background(), hydrateCmd())
	if res.Success || res.ErrorCode != session.CommandErrorInvalidState {
		t.Fatalf("HydrateTrigger on running server = %+v, want invalid-state failure", res)
	}
	if len(tr.hydrated) != 0 {
		t.Fatal("a running server must not be hydrated")
	}
}

func TestHydrateTriggerTransferFailureIsCoded(t *testing.T) {
	tr := &fakeTransfer{err: errors.New("boom")}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	res := m.Handle(context.Background(), hydrateCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("HydrateTrigger failure = %+v, want transfer-failed", res)
	}
}

func TestHydrateTriggerWithoutTransferClientFails(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil) // no WithTransfer
	res := m.Handle(context.Background(), hydrateCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("HydrateTrigger with no client = %+v, want transfer-failed", res)
	}
}

func TestSnapshotTriggerPacksWorkingDir(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	res := m.Handle(context.Background(), snapshotCmd())
	if !res.Success {
		t.Fatalf("SnapshotTrigger result = %+v, want success", res)
	}
	want := filepath.Join(m.scratchDir, "s1")
	if len(tr.snapshots) != 1 || tr.snapshots[0] != want {
		t.Fatalf("snapshots = %v, want [%q]", tr.snapshots, want)
	}
}

func TestSnapshotTriggerTransferFailureIsCoded(t *testing.T) {
	tr := &fakeTransfer{err: errors.New("boom")}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger failure = %+v, want transfer-failed", res)
	}
}
