package instancemanager

import (
	"context"
	"os"
	"sync"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// The launch hand-over proves a multiply-linked file is the working set's own by
// counting its names, which only holds while no name moves under the walk. The
// Worker's own writers publish by renaming a temporary name into place, so each
// of them holds the server's working-set lock from before its temp exists until
// after the rename, and the driver is handed that same lock to hold while it
// walks (issue #3371).

// heldWhenTempDrawn replaces a temp-name seam with one that also records whether
// serverID's working-set lock is held at that point: after the writer began, and
// before it creates the temp it will rename.
func heldWhenTempDrawn(t *testing.T, m *Manager, serverID string, seam *func() string) *[]bool {
	t.Helper()
	orig := *seam
	t.Cleanup(func() { *seam = orig })
	lock := m.workingSetLock(serverID)
	var held []bool
	*seam = func() string {
		free := lock.TryLock()
		if free {
			lock.Unlock()
		}
		held = append(held, !free)
		return orig()
	}
	return &held
}

// assertHeldThenReleased fails unless the lock was held at every temp draw, at
// least one happened, and the lock is free again now.
func assertHeldThenReleased(t *testing.T, m *Manager, serverID string, held []bool) {
	t.Helper()
	if len(held) == 0 {
		t.Fatal("no temp name was drawn; the write under test did not run")
	}
	for i, h := range held {
		if !h {
			t.Fatalf("temp draw %d ran without the working-set lock: a hand-over could walk the "+
				"working set between this temp's creation and its rename", i)
		}
	}
	lock := m.workingSetLock(serverID)
	if !lock.TryLock() {
		t.Fatal("the working-set lock is still held after the command returned")
	}
	lock.Unlock()
}

func TestEditFileHoldsTheWorkingSetLockWhileItPublishes(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	target := writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=old"))
	held := heldWhenTempDrawn(t, m, "s1", &editTempSuffix)

	res := handleWithin(t, m, session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile", Path: "server.properties", Content: []byte("motd=new"),
	})

	if !res.Success {
		t.Fatalf("EditFile = %+v, want success", res)
	}
	assertHeldThenReleased(t, m, "s1", *held)
	if got, err := os.ReadFile(target); err != nil || string(got) != "motd=new" {
		t.Fatalf("the target reads %q (%v), want the edit published", got, err)
	}
}

// A refused edit releases the lock too: here the target is a directory.
func TestEditFileReleasesTheWorkingSetLockWhenItFails(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	writeWorkingFile(t, m, "s1", "world/level.dat", []byte("x"))

	res := handleWithin(t, m, session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile", Path: "world", Content: []byte("x"),
	})

	if res.Success {
		t.Fatalf("EditFile = %+v, want the directory refused", res)
	}
	lock := m.workingSetLock("s1")
	if !lock.TryLock() {
		t.Fatal("the working-set lock is still held after the refused edit returned")
	}
	lock.Unlock()
}

func TestHydrateStampHoldsTheWorkingSetLock(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{gen: 11})
	held := heldWhenTempDrawn(t, m, "s1", &generationTempSuffix)

	if res := m.Handle(context.Background(), hydrateCmd()); !res.Success {
		t.Fatalf("HydrateTrigger = %+v, want success", res)
	}

	assertHeldThenReleased(t, m, "s1", *held)
}

func TestRunningSnapshotStampHoldsTheWorkingSetLock(t *testing.T) {
	m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{gen: 12})
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	held := heldWhenTempDrawn(t, m, "s1", &generationTempSuffix)

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}

	assertHeldThenReleased(t, m, "s1", *held)
}

// The driver walks the working set in places the manager cannot see — a Forge
// install's supervisor creates the launch container long after Start returned —
// so the lock travels to it in the spec, and it is the one the manager's own
// writers take for that server and no other.
func TestStartHandsTheDriverTheServersWorkingSetLock(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	seedScratch(t, m, "s1")

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}

	d.mu.Lock()
	got := d.started[0].WorkingSetLock
	d.mu.Unlock()
	if got != sync.Locker(m.workingSetLock("s1")) {
		t.Fatalf("spec.WorkingSetLock = %p, want server s1's working-set lock %p", got, m.workingSetLock("s1"))
	}
	if m.workingSetLock("s1") == m.workingSetLock("s2") {
		t.Fatal("two servers share one working-set lock; a hand-over of one would hold up edits to the other")
	}
}
