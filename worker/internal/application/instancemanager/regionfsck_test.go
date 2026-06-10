package instancemanager

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

const fsckSector = 4096

// healthyRegion is a minimal structurally-sound region image: an 8 KiB header
// (location + timestamp tables) plus one chunk in sector 2.
func healthyRegion() []byte {
	image := make([]byte, 3*fsckSector)
	// Location-table entry 0: offset sector 2, one sector.
	image[2] = 2
	image[3] = 1
	// Chunk prefix at sector 2: length fills the sector, compression zlib (2).
	start := 2 * fsckSector
	length := uint32(fsckSector - 4)
	image[start] = byte(length >> 24)
	image[start+1] = byte(length >> 16)
	image[start+2] = byte(length >> 8)
	image[start+3] = byte(length)
	image[start+4] = 2
	return image
}

// seedWorkingSet writes data into <scratch>/<serverID>/region/r.0.0.mca.
func seedWorkingSet(t *testing.T, m *Manager, serverID string, data []byte) {
	t.Helper()
	dir := filepath.Join(m.scratchDir, serverID, "region")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatalf("mkdir working set: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "r.0.0.mca"), data, 0o640); err != nil {
		t.Fatalf("write region: %v", err)
	}
}

// A snapshot over a working set with a structurally corrupt region is refused
// before the pack/upload: the pre-pack fsck flags it and handleSnapshot returns a
// coded transfer-failed error, so no transfer happens (#741, fail fast at the
// source rather than after a full tar+upload the API gate would reject anyway).
func TestSnapshotTriggerCorruptWorkingSetRefusedPrePack(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	// A torn region (size not a 4096 multiple) — the #703 reproduction shape.
	seedWorkingSet(t, m, "s1", healthyRegion()[:3*fsckSector-10])

	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger over corrupt set = %+v, want transfer-failed", res)
	}
	if len(tr.snapshots) != 0 {
		t.Fatalf("corrupt working set must not be packed/uploaded; snapshots = %v", tr.snapshots)
	}
}

// A snapshot over a structurally sound working set proceeds: the pre-pack fsck
// passes and the transfer runs.
func TestSnapshotTriggerHealthyWorkingSetProceeds(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	seedWorkingSet(t, m, "s1", healthyRegion())

	res := m.Handle(context.Background(), snapshotCmd())
	if !res.Success {
		t.Fatalf("SnapshotTrigger over healthy set = %+v, want success", res)
	}
	if len(tr.snapshots) != 1 {
		t.Fatalf("healthy working set must be uploaded; snapshots = %v", tr.snapshots)
	}
}

// A running-server snapshot runs the pre-pack fsck inside the #694 save-off/save-on
// bracket: on corruption it still re-enables auto-save (the deferred save-on) and
// refuses the upload, so the server is never left with auto-save disabled.
func TestSnapshotTriggerCorruptRunningServerRefusesAndRestoresSaveOn(t *testing.T) {
	ctrl := &fakeControl{reply: "ok"}
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	seedWorkingSet(t, m, "s1", healthyRegion()[:3*fsckSector-10])

	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger over corrupt running set = %+v, want transfer-failed", res)
	}
	if len(tr.snapshots) != 0 {
		t.Fatalf("corrupt working set must not be uploaded; snapshots = %v", tr.snapshots)
	}
	// save-off quiesced, then save-on re-enabled auto-save despite the refusal.
	if !containsLine(ctrl.lines, "save-off") || !containsLine(ctrl.lines, "save-on") {
		t.Fatalf("rcon lines = %v, want both save-off and save-on around the refused snapshot", ctrl.lines)
	}
}
