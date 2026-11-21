package instancemanager

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"

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
	var synced []string
	orig := fsyncFd
	fsyncFd = func(fd int) error {
		path, err := os.Readlink("/proc/self/fd/" + strconv.Itoa(fd))
		if err != nil {
			t.Errorf("readlink fd %d: %v", fd, err)
		}
		synced = append(synced, path)
		return nil
	}
	t.Cleanup(func() { fsyncFd = orig })
	return &synced
}

// A snapshot that stamps the generation marker makes the tree it packed durable
// first (issue #3999). The server writes its world through the page cache, so a
// marker made durable over an unsynced tree can outlive the tree on a power loss:
// the skip-hydrate gate then boots the older world the marker does not describe,
// and its next snapshot publishes that world over newer data. The running-id
// snapshot syncs right after the pack, while the world is still quiesced, and
// stamps after the upload. The stopped-id snapshot stamps nothing (it removes the
// scratch), so it has no tree to make durable for a marker.
func TestSnapshotMakesThePackedTreeDurableBeforeTheMarker(t *testing.T) {
	for _, tc := range []struct {
		name    string
		running bool
		wantSeq []string
	}{
		{"running", true, []string{"save-off", "save-all", "pack", "sync", "save-on", "upload"}},
		{"stopped", false, []string{"transfer"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var seq []string
			ctrl := &fakeControl{reply: "ok", seq: &seq}
			tr := &fakeTransfer{gen: 12, seq: &seq}
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
				return nil
			})

			res := m.Handle(context.Background(), snapshotCmd())

			if !res.Success {
				t.Fatalf("SnapshotTrigger = %+v, want success", res)
			}
			if !equalLines(seq, tc.wantSeq) {
				t.Fatalf("operation order = %v, want %v", seq, tc.wantSeq)
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
			if got := readGeneration(dir); got != 12 {
				t.Fatalf("marker = %d after the snapshot, want 12", got)
			}
		})
	}
}

// A sync that fails leaves the marker where it was (issue #3999). The publish
// stands: the store holds what the pack read, which is the newest world there is.
// What the Worker cannot claim is that its scratch holds it after a power loss,
// so it neither stamps nor declares the generation; the next start hydrates.
func TestRunningSnapshotDoesNotStampAPackedTreeItCouldNotMakeDurable(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(tr)
	dir := seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	if err := writeGeneration(dir, 5); err != nil {
		t.Fatal(err)
	}
	stubSyncfs(t, func(int) error { return unix.EIO })

	res := m.Handle(context.Background(), snapshotCmd())

	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success: the publish does not depend on the local sync", res)
	}
	if len(tr.uploads) != 1 {
		t.Fatalf("uploads = %v, want one", tr.uploads)
	}
	if got := readGeneration(dir); got != 5 {
		t.Fatalf("marker = %d, want 5: a tree that may not be on disk was stamped as generation 12", got)
	}
	if res.HeldGeneration != nil {
		t.Fatalf("declared held generation %d for a tree that may not be on disk", *res.HeldGeneration)
	}
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
// Worker's shared descriptor table out.
func TestSyncWorkingSetRefusesATreeDeeperThanThePackTakes(t *testing.T) {
	dir := t.TempDir()
	deep := dir
	for range maxSyncDepth + 1 {
		deep = filepath.Join(deep, "d")
	}
	if err := os.MkdirAll(deep, 0o750); err != nil {
		t.Fatal(err)
	}
	stubSyncfs(t, func(int) error { return unix.ENOSYS })
	recordFsyncs(t)

	if err := syncWorkingSet(context.Background(), dir); err == nil {
		t.Fatal("syncWorkingSet = nil, want a refusal of the over-deep tree")
	}
}

// The walk stops once its command's context is done, as the pack does.
func TestSyncWorkingSetWalkStopsWithItsContext(t *testing.T) {
	dir := t.TempDir()
	seedSyncTree(t, dir)
	stubSyncfs(t, func(int) error { return unix.ENOSYS })
	synced := recordFsyncs(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	if err := syncWorkingSet(ctx, dir); !errors.Is(err, context.Canceled) {
		t.Fatalf("syncWorkingSet = %v, want %v", err, context.Canceled)
	}
	if len(*synced) != 0 {
		t.Fatalf("fsynced %v after the context was done", *synced)
	}
}
