package instancemanager

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// stubSyncfs answers every syncfs call of the test with fn.
func stubSyncfs(t *testing.T, fn func(fd int) error) {
	t.Helper()
	orig := syncfs
	syncfs = fn
	t.Cleanup(func() { syncfs = orig })
}

// recordFsyncs records the path of every descriptor the tree walk fsyncs, and
// fsyncs nothing.
func recordFsyncs(t *testing.T) *[]string {
	t.Helper()
	return stubFsyncs(t, func(string) error { return nil })
}

// stubFsyncs records the path of every descriptor the tree walk fsyncs and answers
// each fsync with answer(path), fsyncing nothing.
func stubFsyncs(t *testing.T, answer func(path string) error) *[]string {
	t.Helper()
	var synced []string
	orig := fsyncFd
	fsyncFd = func(fd int) error {
		path, err := os.Readlink("/proc/self/fd/" + strconv.Itoa(fd))
		if err != nil {
			t.Errorf("readlink fd %d: %v", fd, err)
		}
		synced = append(synced, path)
		return answer(path)
	}
	t.Cleanup(func() { fsyncFd = orig })
	return &synced
}

// recordStamps appends "stamp" to seq each time a generation-marker write starts
// (its temp name is drawn).
func recordStamps(t *testing.T, seq *[]string) {
	t.Helper()
	orig := generationTempSuffix
	generationTempSuffix = func() string {
		*seq = append(*seq, "stamp")
		return orig()
	}
	t.Cleanup(func() { generationTempSuffix = orig })
}

// A snapshot that stamps the generation marker makes the tree durable first (issue
// #3999). The server writes its world through the page cache, so a marker made
// durable over an unsynced tree can outlive the tree on a power loss: the
// skip-hydrate gate then boots the older world the marker does not describe, and
// its next snapshot publishes that world over newer data.
//
// The running-id snapshot syncs after the upload, outside the save-off window: a
// sync started then flushes everything the tree held at the pack and whatever the
// server wrote since, so the durable tree is no older than the generation the stamp
// names. It removes the spool first, so the sync drops the spool's pages instead of
// writing them. A sync that fails still publishes but does not stamp. The stopped-id
// snapshot stamps nothing (it removes the scratch), so it syncs nothing.
func TestSnapshotMakesTheTreeDurableBeforeTheMarker(t *testing.T) {
	quiesced := []string{"save-off", "save-all", "pack", "save-on", "upload", "spool removed", "sync"}
	for _, tc := range []struct {
		name     string
		running  bool
		syncErr  error
		fsyncErr error // what the fallback walk's fsyncs answer, where syncErr sends it there
		wantSeq  []string
		wantMark uint64
	}{
		{"running", true, nil, nil, append(quiesced[:len(quiesced):len(quiesced)], "stamp"), 12},
		{"running, sync fails", true, unix.EIO, nil, quiesced, 5},
		{"running, the walk's fsync fails", true, unix.ENOSYS, unix.EIO, quiesced, 5},
		{"stopped", false, nil, nil, []string{"transfer"}, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var seq []string
			ctrl := &fakeControl{reply: "ok", seq: &seq}
			tr := &fakeTransfer{gen: 12, seq: &seq}
			// The hook runs as the spool is removed.
			tr.stallCleanup = func() { seq = append(seq, "spool removed") }
			m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
			dir := seedScratch(t, m, s1)
			if tc.running {
				if res := m.Handle(context.Background(), startCmd()); !res.Success {
					t.Fatalf("start = %+v, want success", res)
				}
			}
			if err := writeGeneration(dir, 5); err != nil {
				t.Fatal(err)
			}
			var markerAtSync []uint64
			stubSyncfs(t, func(int) error {
				seq = append(seq, "sync")
				markerAtSync = append(markerAtSync, readGeneration(dir))
				return tc.syncErr
			})
			stubFsyncs(t, func(string) error { return tc.fsyncErr })
			recordStamps(t, &seq)

			res := m.Handle(context.Background(), snapshotCmd())

			if !res.Success {
				t.Fatalf("SnapshotTrigger = %+v, want success: the publish does not depend on the local sync", res)
			}
			// Errorf, not Fatalf: an order that is wrong but still stamps the right
			// marker must report the order alone.
			if !equalLines(seq, tc.wantSeq) {
				t.Errorf("operation order = %v, want %v", seq, tc.wantSeq)
			}
			if !tc.running {
				if _, err := os.Stat(dir); !os.IsNotExist(err) {
					t.Fatalf("stopped-id scratch not removed (stat err = %v), so a marker may survive it", err)
				}
				return
			}
			if len(markerAtSync) != 1 || markerAtSync[0] != 5 {
				t.Fatalf("marker read %v when the tree was synced, want [5]: the marker claimed the "+
					"published generation before the tree it names was durable", markerAtSync)
			}
			if got := readGeneration(dir); got != tc.wantMark {
				t.Fatalf("marker = %d after the snapshot, want %d", got, tc.wantMark)
			}
			declared := res.HeldGeneration != nil
			if wantDeclared := tc.wantMark == 12; declared != wantDeclared {
				t.Fatalf("declared a held generation: %v, want %v (it must follow the stamp)", declared, wantDeclared)
			}
		})
	}
}

// The tail's sync runs on the request's context, not the command's deadline (issue
// #3999): it belongs to the committed tail, which may outlive that deadline, as the
// stamp it gates does. A walk cut short by the deadline would withhold a stamp over
// a tree about to be durable. Here the deadline passes as the sync starts, and the
// fallback walk, which stops with its context, must still finish and let the stamp
// land.
func TestRunningSnapshotSyncsOnTheRequestContextNotTheDeadline(t *testing.T) {
	m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{gen: 12})
	dir := seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	if err := writeGeneration(dir, 5); err != nil {
		t.Fatal(err)
	}
	var endDeadline context.CancelFunc
	m.snapshotContextHook = func(ctx context.Context, _ bool) (context.Context, context.CancelFunc) {
		var cmdCtx context.Context
		cmdCtx, endDeadline = context.WithCancel(ctx)
		return cmdCtx, endDeadline
	}
	exited := make(chan struct{})
	m.snapshotBodyExited = func(string) { close(exited) }
	stubSyncfs(t, func(int) error {
		endDeadline()
		return unix.ENOSYS
	})
	recordFsyncs(t)

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success: the upload had published", res)
	}
	<-exited

	if got := readGeneration(dir); got != 12 {
		t.Fatalf("marker = %d, want 12: the sync stopped with the command's deadline instead of "+
			"running on the request's context", got)
	}
}

// A running snapshot logs how long its sync took (issue #3999), so the cost of the
// sync on a real host can be read off the Worker's log.
func TestRunningSnapshotLogsHowLongTheSyncTook(t *testing.T) {
	h := &capturingSlogHandler{}
	m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{gen: 12})
	seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	m.WithLogger(slog.New(h))
	stubSyncfs(t, func(int) error { return nil })

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}

	for _, r := range h.records {
		var serverID string
		var syncMs bool
		r.Attrs(func(a slog.Attr) bool {
			switch a.Key {
			case "server_id":
				serverID = a.Value.String()
			case "sync_ms":
				syncMs = a.Value.Kind() == slog.KindInt64
			}
			return true
		})
		if r.Level == slog.LevelInfo && serverID == s1 && syncMs {
			return
		}
	}
	t.Fatalf("no INFO line carrying sync_ms for %s; records = %v", s1, h.records)
}

// seedSyncTree builds a small working set under dir holding everything the walk
// meets: nested directories, regular files, a FIFO (which must not block it) and a
// symlink out of the tree (which must not be followed). It returns the paths the
// walk must fsync and the directory the symlink points at.
func seedSyncTree(t *testing.T, dir string) (want []string, outside string) {
	t.Helper()
	outside = t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	region := filepath.Join(dir, "world", "region")
	if err := os.MkdirAll(region, 0o750); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"level.dat", filepath.Join("world", "level.dat"), filepath.Join("world", "region", "r.0.0.mca")} {
		if err := os.WriteFile(filepath.Join(dir, f), []byte("x"), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	if err := unix.Mkfifo(filepath.Join(dir, "world", "pipe"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(dir, "escape")); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"", "level.dat", "world", filepath.Join("world", "level.dat"), filepath.Join("world", "region"), filepath.Join("world", "region", "r.0.0.mca")} {
		want = append(want, filepath.Join(dir, p))
	}
	sort.Strings(want)
	return want, outside
}

// Where syncfs is unavailable — an old kernel answers ENOSYS, and a seccomp
// profile that does not know it answers EPERM — the tree is made durable by
// fsyncing every regular file and directory in it instead (issue #3999). The
// server wrote those files, so unlike the hydrate's directory-only walk this one
// syncs the files' contents too. It does not follow a link or open a FIFO.
func TestSyncWorkingSetFsyncsTheTreeWhereSyncfsIsUnavailable(t *testing.T) {
	for _, errno := range []error{unix.ENOSYS, unix.EPERM} {
		t.Run(errno.Error(), func(t *testing.T) {
			dir := t.TempDir()
			want, outside := seedSyncTree(t, dir)
			stubSyncfs(t, func(int) error { return errno })
			synced := recordFsyncs(t)

			if err := syncWorkingSet(context.Background(), dir); err != nil {
				t.Fatalf("syncWorkingSet = %v, want nil", err)
			}

			got := append([]string(nil), *synced...)
			sort.Strings(got)
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("fsynced %v, want %v", got, want)
			}
			for _, p := range got {
				if strings.HasPrefix(p, outside) {
					t.Fatalf("fsynced %q through the symlink out of the tree", p)
				}
			}
			if last := (*synced)[len(*synced)-1]; last != dir {
				t.Fatalf("last fsync = %q, want the working dir itself, after everything below it", last)
			}
		})
	}
}

// Where syncfs answers, its answer is the sync's: success needs no walk, and any
// other failure is returned as it is rather than retried as a walk, which would
// report a failed sync as a durable tree.
func TestSyncWorkingSetWalksOnlyWhereSyncfsIsUnavailable(t *testing.T) {
	for _, tc := range []struct {
		name   string
		answer error
	}{
		{"success", nil},
		{"EIO", unix.EIO},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			seedSyncTree(t, dir)
			calls := 0
			stubSyncfs(t, func(int) error { calls++; return tc.answer })
			synced := recordFsyncs(t)

			err := syncWorkingSet(context.Background(), dir)

			if !errors.Is(err, tc.answer) || (tc.answer == nil && err != nil) {
				t.Fatalf("syncWorkingSet = %v, want %v", err, tc.answer)
			}
			if calls != 1 {
				t.Fatalf("syncfs called %d times, want 1", calls)
			}
			if len(*synced) != 0 {
				t.Fatalf("fsynced %v, want no walk", *synced)
			}
		})
	}
}

// The walk holds one descriptor per level, and the tree's shape is the server's
// to choose, so it refuses a tree deeper than the pack takes rather than run the
// Worker's shared descriptor table out. A tree as deep as the pack takes is walked:
// refusing it would leave a server whose snapshots publish never stamped.
func TestSyncWorkingSetWalksATreeAsDeepAsThePackTakesAndNoDeeper(t *testing.T) {
	for _, tc := range []struct {
		levels int
		refuse bool
	}{
		{maxSyncDepth, false},
		{maxSyncDepth + 1, true},
	} {
		t.Run(strconv.Itoa(tc.levels), func(t *testing.T) {
			dir := t.TempDir()
			deep := dir
			for range tc.levels {
				deep = filepath.Join(deep, "d")
			}
			if err := os.MkdirAll(deep, 0o750); err != nil {
				t.Fatal(err)
			}
			stubSyncfs(t, func(int) error { return unix.ENOSYS })
			synced := recordFsyncs(t)

			err := syncWorkingSet(context.Background(), dir)

			if tc.refuse && err == nil {
				t.Fatal("syncWorkingSet = nil, want a refusal of the over-deep tree")
			}
			if !tc.refuse && (err != nil || len(*synced) != tc.levels+1) {
				t.Fatalf("syncWorkingSet = %v after %d fsyncs, want nil after %d", err, len(*synced), tc.levels+1)
			}
		})
	}
}

// A walk whose fsync fails, on a file or on a directory, reports it (issue #3999):
// swallowing the error would report a tree that may not be on the disk as durable,
// and the snapshot would stamp it.
func TestSyncWorkingSetReportsAFailedFsync(t *testing.T) {
	for _, target := range []string{filepath.Join("world", "region", "r.0.0.mca"), filepath.Join("world", "region")} {
		t.Run(target, func(t *testing.T) {
			dir := t.TempDir()
			seedSyncTree(t, dir)
			stubSyncfs(t, func(int) error { return unix.ENOSYS })
			failing := filepath.Join(dir, target)
			stubFsyncs(t, func(path string) error {
				if path == failing {
					return unix.EIO
				}
				return nil
			})

			if err := syncWorkingSet(context.Background(), dir); !errors.Is(err, unix.EIO) {
				t.Fatalf("syncWorkingSet = %v, want the fsync's %v", err, unix.EIO)
			}
		})
	}
}

// An entry replaced by a link or a FIFO between the listing and its open is neither
// followed, nor blocked on, nor fsynced: the open does not follow a link nor wait
// for a FIFO's writer, and only what fstat finds regular is synced.
func TestSyncWorkingSetSkipsAnEntryReplacedAfterTheListing(t *testing.T) {
	for _, tc := range []struct {
		name  string
		plant func(path, outside string) error
	}{
		{"symlink", func(path, outside string) error { return os.Symlink(filepath.Join(outside, "secret"), path) }},
		{"fifo", func(path, _ string) error { return unix.Mkfifo(path, 0o640) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir, outside := t.TempDir(), t.TempDir()
			for _, f := range []string{filepath.Join(dir, "a"), filepath.Join(dir, "b"), filepath.Join(outside, "secret")} {
				if err := os.WriteFile(f, []byte("x"), 0o640); err != nil {
					t.Fatal(err)
				}
			}
			stubSyncfs(t, func(int) error { return unix.ENOSYS })
			// The first file synced replaces the other, which the walk has listed as a
			// regular file and not yet opened.
			replaced := false
			synced := stubFsyncs(t, func(path string) error {
				if replaced {
					return nil
				}
				replaced = true
				other := filepath.Join(dir, "a")
				if path == other {
					other = filepath.Join(dir, "b")
				}
				if err := os.Remove(other); err != nil {
					return err
				}
				return tc.plant(other, outside)
			})
			done := make(chan error, 1)

			go func() { done <- syncWorkingSet(context.Background(), dir) }()

			select {
			case err := <-done:
				if err != nil {
					t.Fatalf("syncWorkingSet = %v, want nil", err)
				}
			case <-time.After(10 * time.Second):
				t.Fatal("syncWorkingSet blocked on the replaced entry")
			}
			if len(*synced) != 2 || (*synced)[1] != dir {
				t.Fatalf("fsynced %v, want the first file and the working dir only", *synced)
			}
		})
	}
}

// The walk checks its context before each entry, as the pack does, so a walk whose
// context ends part way stops at the next entry rather than at the next directory.
func TestSyncWorkingSetWalkStopsWithItsContext(t *testing.T) {
	dir := t.TempDir()
	for _, f := range []string{"a", "b", "c"} {
		if err := os.WriteFile(filepath.Join(dir, f), []byte("x"), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	stubSyncfs(t, func(int) error { return unix.ENOSYS })
	ctx, cancel := context.WithCancel(context.Background())
	synced := stubFsyncs(t, func(string) error { cancel(); return nil })

	if err := syncWorkingSet(ctx, dir); !errors.Is(err, context.Canceled) {
		t.Fatalf("syncWorkingSet = %v, want %v", err, context.Canceled)
	}
	if len(*synced) != 1 {
		t.Fatalf("fsynced %v after the context ended during the first, want that one only", *synced)
	}
}
