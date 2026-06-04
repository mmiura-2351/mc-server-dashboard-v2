package instancemanager

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// listNames returns the entry names of a successful ListFiles result, sorted, so
// assertions do not depend on directory iteration order.
func listNames(t *testing.T, res session.CommandResult) []string {
	t.Helper()
	if !res.Success {
		t.Fatalf("ListFiles result = %+v, want success", res)
	}
	if res.FileListing == nil {
		t.Fatal("FileListing is nil on a successful ListFiles")
	}
	names := make([]string, 0, len(res.FileListing.Entries))
	for _, e := range res.FileListing.Entries {
		names = append(names, e.Name)
	}
	sort.Strings(names)
	return names
}

func TestListFilesReturnsEntries(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=hi"))
	writeWorkingFile(t, m, "s1", "world/level.dat", []byte("data"))

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ListFiles", Path: ".",
	})
	if got := listNames(t, res); len(got) != 2 || got[0] != "server.properties" || got[1] != "world" {
		t.Fatalf("entries = %v, want [server.properties world]", got)
	}
	if res.FileListing.Truncated {
		t.Fatal("Truncated set on a small listing")
	}

	// Sizes / types: server.properties is a 7-byte file, world is a directory.
	byName := map[string]session.FileEntry{}
	for _, e := range res.FileListing.Entries {
		byName[e.Name] = e
	}
	if e := byName["server.properties"]; e.IsDir || e.Size != 7 {
		t.Fatalf("server.properties entry = %+v, want file size 7", e)
	}
	if e := byName["world"]; !e.IsDir || e.Size != 0 {
		t.Fatalf("world entry = %+v, want dir size 0", e)
	}
}

func TestListFilesEmptyDirReturnsEmptyListing(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	if err := os.MkdirAll(filepath.Join(m.scratchDir, "s1", "plugins"), 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ListFiles", Path: "plugins",
	})
	if !res.Success || res.FileListing == nil {
		t.Fatalf("ListFiles result = %+v, want success with a listing", res)
	}
	if len(res.FileListing.Entries) != 0 {
		t.Fatalf("entries = %v, want empty", res.FileListing.Entries)
	}
}

func TestListFilesMissingDirIsServerNotFound(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ListFiles", Path: "nope",
	})
	if res.Success {
		t.Fatal("ListFiles of a missing dir should fail")
	}
	if res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("ErrorCode = %v, want CommandErrorServerNotFound", res.ErrorCode)
	}
}

func TestListFilesMissingWorkingDirIsServerNotFound(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	// No working dir created at all: listing the root must not error internally.
	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ListFiles", Path: ".",
	})
	if res.Success {
		t.Fatal("ListFiles of an absent working dir should fail")
	}
	if res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("ErrorCode = %v, want CommandErrorServerNotFound", res.ErrorCode)
	}
}

func TestListFilesOnRegularFileIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=hi"))

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ListFiles", Path: "server.properties",
	})
	if res.Success {
		t.Fatal("ListFiles of a regular file should be denied")
	}
	if res.ErrorCode != session.CommandErrorFileAccessDenied {
		t.Fatalf("ErrorCode = %v, want FileAccessDenied", res.ErrorCode)
	}
}

func TestListFilesTraversalIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	for _, bad := range []string{"../escape", "/etc", "a/../../escape"} {
		res := m.Handle(context.Background(), session.Command{
			CommandID: "c1", ServerID: "s1", Kind: "ListFiles", Path: bad,
		})
		if res.Success {
			t.Fatalf("ListFiles %q should be denied", bad)
		}
		if res.ErrorCode != session.CommandErrorFileAccessDenied {
			t.Fatalf("ListFiles %q ErrorCode = %v, want FileAccessDenied", bad, res.ErrorCode)
		}
	}
}

func TestListFilesSymlinkDirEscapeIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	workingDir := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	// A symlink the running MC process could plant, pointing outside the root.
	outsideDir := t.TempDir()
	if err := os.Symlink(outsideDir, filepath.Join(workingDir, "evil")); err != nil {
		t.Fatalf("symlink: %v", err)
	}
	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ListFiles", Path: "evil",
	})
	if res.Success {
		t.Fatal("ListFiles through a symlink should be denied")
	}
	if res.ErrorCode != session.CommandErrorFileAccessDenied {
		t.Fatalf("ErrorCode = %v, want FileAccessDenied", res.ErrorCode)
	}
}

func TestListFilesBoundedWithTruncationMarker(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	dir := filepath.Join(m.scratchDir, "s1", "many")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	// One more entry than the cap so the listing is clipped and flagged.
	for i := 0; i < MaxDirEntries+1; i++ {
		f := filepath.Join(dir, fmt.Sprintf("f%05d", i))
		if err := os.WriteFile(f, nil, 0o640); err != nil {
			t.Fatalf("seed %d: %v", i, err)
		}
	}
	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ListFiles", Path: "many",
	})
	if !res.Success || res.FileListing == nil {
		t.Fatalf("ListFiles result = %+v, want success with a listing", res)
	}
	if len(res.FileListing.Entries) != MaxDirEntries {
		t.Fatalf("entries = %d, want %d (capped)", len(res.FileListing.Entries), MaxDirEntries)
	}
	if !res.FileListing.Truncated {
		t.Fatal("Truncated not set on an over-cap listing")
	}
}
