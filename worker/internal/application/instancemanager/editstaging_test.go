package instancemanager

import (
	"archive/tar"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/datatransfer"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// errCrash stands in for a Worker death inside an edit.
var errCrash = errors.New("the worker died here")

// editedServerID names the working set the stranded-edit test edits. It is a UUID
// because the held-set scan advertises only directories named by a server id (issue
// #3395).
const editedServerID = "0e000000-0000-4000-8000-000000000001"

// crashBeforePublish runs an edit of editedServerID's server.properties that dies
// after its file was given its staged name and before the rename: the panic skips
// every clean-up an orderly failure runs, as a crash does.
func crashBeforePublish(t *testing.T, m *Manager) {
	t.Helper()
	parentFd, leaf, err := openParentBeneath(filepath.Join(m.scratchDir, editedServerID),
		filepath.Join(m.scratchDir, editedServerID, "server.properties"), true)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = unix.Close(parentFd) }()
	stageFd, err := unix.Open(m.scratchDir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = unix.Close(stageFd) }()

	orig := fchownFd
	t.Cleanup(func() { fchownFd = orig })
	fchownFd = func(int, int, int) error { panic(errCrash) }
	defer func() {
		if r := recover(); r != errCrash {
			t.Fatalf("the edit did not reach the point of the crash (recovered %v)", r)
		}
	}()
	_ = atomicWriteAt(stageFd, parentFd, leaf, []byte("motd=stranded"))
}

// A crash between naming an edit's file and publishing it strands the staged
// name (issue #3359). It is in the scratch root, so the working set's snapshot
// does not pack it and no held-set scan counts it, and the next boot removes it —
// and nothing else: a server's own file under the same prefix is the server's.
func TestAStrandedEditStagingIsNotPackedAndIsReclaimedAtBoot(t *testing.T) {
	stagingModes(t, func(t *testing.T) {
		m := newManager(t, &fakeDriver{}, nil)
		writeWorkingFile(t, m, editedServerID, "server.properties", []byte("motd=old"))
		lookalike := writeWorkingFile(t, m, editedServerID, ".edit-lookalike", []byte("the server's own"))
		unrelated := filepath.Join(m.scratchDir, "unrelated")
		if err := os.WriteFile(unrelated, nil, 0o600); err != nil {
			t.Fatal(err)
		}

		crashBeforePublish(t, m)

		var stranded []string
		for _, name := range dirNames(t, m.scratchDir) {
			if strings.HasPrefix(name, editStagePrefix) {
				stranded = append(stranded, name)
			}
		}
		if len(stranded) != 1 {
			t.Fatalf("staged names left in the scratch root = %v, want the one the crash stranded", stranded)
		}

		if got, want := packedNames(t, filepath.Join(m.scratchDir, editedServerID)), []string{".edit-lookalike", "server.properties"}; !reflect.DeepEqual(got, want) {
			t.Fatalf("the snapshot packs %v, want %v", got, want)
		}
		if got, want := m.HeldServers(), []session.HeldServer{{ServerID: editedServerID}}; !reflect.DeepEqual(got, want) {
			t.Fatalf("held servers = %v, want %v", got, want)
		}

		ReclaimEditStaging(m.scratchDir)

		if _, err := os.Lstat(filepath.Join(m.scratchDir, stranded[0])); !os.IsNotExist(err) {
			t.Fatalf("the stranded staged file is still there after the boot reclaim (%v)", err)
		}
		for path, want := range map[string]string{
			lookalike: "the server's own",
			filepath.Join(m.scratchDir, editedServerID, "server.properties"): "motd=old",
			unrelated: "",
		} {
			if got, err := os.ReadFile(path); err != nil || string(got) != want {
				t.Errorf("%s reads %q (%v) after the boot reclaim, want %q", path, got, err, want)
			}
		}
	})
}

// After the rename both directories it changed — the target's and the scratch
// root — are fsynced, once the new content is in place. A failure of that sync
// does not undo or fail the published edit.
func TestEditFileSyncsBothDirectoriesAfterThePublish(t *testing.T) {
	for name, syncErr := range map[string]error{"sync succeeds": nil, "sync fails": unix.EIO} {
		t.Run(name, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, nil)
			target := writeWorkingFile(t, m, "s1", "sub/server.properties", []byte("motd=old"))
			orig := syncEditDir
			t.Cleanup(func() { syncEditDir = orig })
			var synced []uint64
			syncEditDir = func(fd int) error {
				if got, err := os.ReadFile(target); err != nil || string(got) != "motd=new" {
					t.Errorf("a directory was synced before the publish: target reads %q (%v)", got, err)
				}
				var st unix.Stat_t
				if err := unix.Fstat(fd, &st); err != nil {
					t.Fatal(err)
				}
				synced = append(synced, st.Ino)
				return syncErr
			}

			res := handleWithin(t, m, session.Command{
				CommandID: "c1", ServerID: "s1", Kind: "EditFile", Path: "sub/server.properties", Content: []byte("motd=new"),
			})

			if !res.Success {
				t.Fatalf("EditFile result = %+v, want success", res)
			}
			want := []uint64{lstat(t, filepath.Dir(target)).Ino, lstat(t, m.scratchDir).Ino}
			if !reflect.DeepEqual(synced, want) {
				t.Fatalf("synced directory inodes %v, want the target's and the scratch root's %v", synced, want)
			}
			assertNoEditTemp(t, m, filepath.Dir(target), "")
		})
	}
}

// packedNames packs workingDir as a snapshot does and returns the member names.
func packedNames(t *testing.T, workingDir string) []string {
	t.Helper()
	spool, cleanup, err := datatransfer.New(nil).PackSnapshot(context.Background(), workingDir)
	if err != nil {
		t.Fatal(err)
	}
	defer cleanup()
	f, err := os.Open(spool)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	var names []string
	tr := tar.NewReader(f)
	for {
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		names = append(names, hdr.Name)
	}
	sort.Strings(names)
	return names
}
