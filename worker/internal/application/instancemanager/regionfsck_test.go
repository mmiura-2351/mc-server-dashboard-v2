package instancemanager

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
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

// unalignedLiveRegion is a structurally-sound region with the legitimate UNPADDED
// tail of a 26.x world (issue #923): an 8 KiB header plus one chunk in sector 2
// whose data ends mid-sector, so the file size is NOT a multiple of 4096 but the
// trailing chunk fits byte-precisely (offset*4096 + 4 + length == size). The single
// rule set (issue #927) accepts it on every path — running OR stopped.
func unalignedLiveRegion() []byte {
	const tail = 459 // partial final sector, mirroring the observed 922,059-byte file.
	size := 2*fsckSector + tail
	image := make([]byte, size)
	image[2] = 2 // location entry 0: offset sector 2.
	image[3] = 1 // one (partial) sector.
	start := 2 * fsckSector
	length := uint32(size - start - 4)
	image[start] = byte(length >> 24)
	image[start+1] = byte(length >> 16)
	image[start+2] = byte(length >> 8)
	image[start+3] = byte(length)
	image[start+4] = 2 // zlib.
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
// refuses the upload, so the server is never left with auto-save disabled. The
// corruption here is persistent (the seeded file never changes), so it survives the
// #907 fsck retries and the snapshot is ultimately refused.
func TestSnapshotTriggerCorruptRunningServerRefusesAndRestoresSaveOn(t *testing.T) {
	ctrl := &fakeControl{reply: "ok"}
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	m.fsckRetryDelay = 0
	m.settlePollInterval = 0

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

// A running-server periodic snapshot retries the pre-pack fsck on transient
// corruption (#907): a region write still in flight just after the save-all flush
// can read as torn, but that must not veto the snapshot. Here the first scan reads
// a torn region; the file is repaired to a healthy region during the retry backoff,
// so a later attempt is clean and the snapshot proceeds.
func TestSnapshotTriggerRunningServerFsckRetriesPastTransientCorruption(t *testing.T) {
	ctrl := &fakeControl{reply: "ok"}
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	m.fsckRetryDelay = 50 * time.Millisecond
	// The settle-wait sees the seeded torn region as already static (the rewrite
	// happens later, during the fsck backoff), so it settles immediately and the
	// fsck retry — not the settle-wait — is what absorbs the transient corruption.
	m.settlePollInterval = 0

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	// Seed a torn region so the first fsck attempt fails.
	region := filepath.Join(m.scratchDir, "s1", "region", "r.0.0.mca")
	seedWorkingSet(t, m, "s1", healthyRegion()[:3*fsckSector-10])

	// Repair the region mid-backoff (after the first attempt, before a later one), as
	// an in-flight write completing would. The 10ms lead vs the 50ms backoff gives the
	// rewrite ample margin before the second scan.
	go func() {
		time.Sleep(10 * time.Millisecond)
		_ = os.WriteFile(region, healthyRegion(), 0o640)
	}()

	res := m.Handle(context.Background(), snapshotCmd())
	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success after the transient corruption clears on retry", res)
	}
	if len(tr.snapshots) != 1 {
		t.Fatalf("snapshots = %v, want one once a retry reads the set clean", tr.snapshots)
	}
}

// The quiesce settle-wait blocks until the async save's region files stop changing
// before the fsck/copy runs (#907): a file still being written between scans delays
// the snapshot, and only once two consecutive scans observe an identical
// (mtime, size) does the snapshot proceed. Here a writer rewrites the region for a
// short burst after save-all, then stops; the settle-wait waits out the burst and
// the snapshot then publishes.
func TestSnapshotTriggerRunningServerSettlesThenProceeds(t *testing.T) {
	ctrl := &fakeControl{reply: "ok"}
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	m.settlePollInterval = 0
	m.settleBudget = 5 * time.Second
	m.fsckRetryDelay = 0
	// The on-disk working set is a healthy, static region; the fsck after the settle
	// reads it clean. The injected scanner reports the region state CHANGING for the
	// first few scans (size growing, the async save still draining), then stable — so
	// the settle-wait must wait out the changes and proceed only once two consecutive
	// scans match. Deterministic — no filesystem race.
	seedWorkingSet(t, m, "s1", healthyRegion())
	var calls int
	m.scanRegion = func(string) (regionState, error) {
		calls++
		size := int64(calls)
		if calls >= 4 {
			size = 4 // stable from the 4th scan on: scans 4 and 5 match -> settled.
		}
		return regionState{"r.0.0.mca": {modTime: time.Unix(0, 0), size: size}}, nil
	}

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}

	res := m.Handle(context.Background(), snapshotCmd())
	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success once the region settles", res)
	}
	if len(tr.snapshots) != 1 {
		t.Fatalf("snapshots = %v, want one once the working set settles", tr.snapshots)
	}
	// The settle-wait must have polled past the changing scans before proceeding, not
	// proceeded on the first scan: at least the prime scan + the scans up to stability.
	if calls < 5 {
		t.Fatalf("scanRegion called %d times, want >=5 (settle-wait waited out the changes)", calls)
	}
}

// The stopped-id (at-rest) snapshot does NOT retry the fsck (#907): the world is at
// rest there, so a detected corruption is real signal, not a mid-write race, and is
// refused fail-closed on the first scan with NO retry backoff.
func TestSnapshotTriggerStoppedServerFsckNotRetried(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	// A non-zero delay that would be observable if the at-rest path ever retried: the
	// test would block on it. The stopped path must not wait at all.
	m.fsckRetryDelay = time.Hour

	seedWorkingSet(t, m, "s1", healthyRegion()[:3*fsckSector-10])

	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger over at-rest corrupt set = %+v, want transfer-failed", res)
	}
	if len(tr.snapshots) != 0 {
		t.Fatalf("at-rest corrupt set must not be uploaded; snapshots = %v", tr.snapshots)
	}
}

// A RUNNING server's periodic snapshot over a working set with the legitimate
// unpadded tail of a 26.x world (non-4096-aligned but byte-precisely valid)
// PROCEEDS: the single rule set treats the unpadded tail as the on-disk format, not
// corruption (#927).
func TestSnapshotTriggerRunningServerUnalignedTailProceeds(t *testing.T) {
	ctrl := &fakeControl{reply: "ok"}
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	m.fsckRetryDelay = 0
	m.settlePollInterval = 0

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	seedWorkingSet(t, m, "s1", unalignedLiveRegion())

	res := m.Handle(context.Background(), snapshotCmd())
	if !res.Success {
		t.Fatalf("SnapshotTrigger over an unaligned working set = %+v, want success", res)
	}
	if len(tr.snapshots) != 1 {
		t.Fatalf("unaligned set must be uploaded; snapshots = %v", tr.snapshots)
	}
}

// The #927 regression case: the STOPPED-id (at-rest) snapshot over the SAME unaligned
// tail now PROCEEDS. The old strict mode refused it on the `stopped => 4096-padded`
// assumption, which does not hold after a sweep-stop timeout / SIGKILL / crash — so
// the stop-leg checkpoint failed exactly when it was the last chance to capture the
// world. The single rule set accepts the byte-precisely-valid unpadded set.
func TestSnapshotTriggerStoppedServerUnalignedTailProceeds(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	seedWorkingSet(t, m, "s1", unalignedLiveRegion())

	res := m.Handle(context.Background(), snapshotCmd())
	if !res.Success {
		t.Fatalf("SnapshotTrigger over an at-rest unaligned set = %+v, want success", res)
	}
	if len(tr.snapshots) != 1 {
		t.Fatalf("at-rest unaligned set must be uploaded; snapshots = %v", tr.snapshots)
	}
}

// When RCON cannot be opened for a RUNNING server, the periodic snapshot is refused
// with the distinct quiesce_unavailable classification (#907) rather than packing
// the unquiesced live world (the 35/35 false-positive source). The next tick
// retries; the post-stop final snapshot still covers a permanently-RCON-broken
// server.
func TestSnapshotTriggerRunningServerRconUnavailableRefusesQuiesceUnavailable(t *testing.T) {
	tr := &fakeTransfer{}
	scratch := t.TempDir()
	openErr := errors.New("dial tcp 127.0.0.1:25575: connect: connection refused")
	m := New(map[string]execution.ExecutionDriver{"host-process": &fakeDriver{}}, scratch,
		func(context.Context, string, string) (execution.ServerControl, error) { return nil, openErr }).
		WithTransfer(tr)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	seedWorkingSet(t, m, "s1", healthyRegion())

	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger with RCON down = %+v, want transfer-failed (quiesce_unavailable)", res)
	}
	if !strings.Contains(res.ErrorMessage, "quiesce_unavailable") {
		t.Fatalf("error message = %q, want it to name quiesce_unavailable", res.ErrorMessage)
	}
	if len(tr.snapshots) != 0 {
		t.Fatalf("unquiesced world must not be packed; snapshots = %v", tr.snapshots)
	}
}
