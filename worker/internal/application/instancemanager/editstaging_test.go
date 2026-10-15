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

// crashBeforePublish runs an edit of s1's server.properties that dies after its
// file was given its staged name and before the rename: the panic skips every
// clean-up an orderly failure runs, as a crash does.
func crashBeforePublish(t *testing.T, m *Manager) {
	t.Helper()
	parentFd, leaf, err := openParentBeneath(filepath.Join(m.scratchDir, "s1"),
		filepath.Join(m.scratchDir, "s1", "server.properties"), true)
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
		writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=old"))
		lookalike := writeWorkingFile(t, m, "s1", ".edit-lookalike", []byte("the server's own"))
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

		if got, want := packedNames(t, filepath.Join(m.scratchDir, "s1")), []string{".edit-lookalike", "server.properties"}; !reflect.DeepEqual(got, want) {
			t.Fatalf("the snapshot packs %v, want %v", got, want)
		}
		if got, want := m.HeldServers(), []session.HeldServer{{ServerID: "s1"}}; !reflect.DeepEqual(got, want) {
			t.Fatalf("held servers = %v, want %v", got, want)
		}

		ReclaimEditStaging(m.scratchDir)

		if _, err := os.Lstat(filepath.Join(m.scratchDir, stranded[0])); !os.IsNotExist(err) {
			t.Fatalf("the stranded staged file is still there after the boot reclaim (%v)", err)
		}
		for path, want := range map[string]string{
			lookalike: "the server's own",
			filepath.Join(m.scratchDir, "s1", "server.properties"): "motd=old",
			unrelated: "",
		} {
			if got, err := os.ReadFile(path); err != nil || string(got) != want {
				t.Errorf("%s reads %q (%v) after the boot reclaim, want %q", path, got, err, want)
			}
		}
	})
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
