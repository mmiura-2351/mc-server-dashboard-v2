package datatransfer

import (
	"archive/tar"
	"bytes"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
)

// referencePackTar is the path-based snapshot walk as it stood before issue #3334
// (lstat, then ReadDir / Open by path), retained here — in the test only — as the
// definition of the archive format. The descriptor-relative walk that replaced it
// must produce the same bytes for a working set nobody is changing.
func referencePackTar(srcDir string, w io.Writer) error {
	root, err := filepath.Abs(srcDir)
	if err != nil {
		return err
	}
	info, err := os.Stat(root)
	if err != nil {
		if os.IsNotExist(err) {
			return tar.NewWriter(w).Close()
		}
		return err
	}
	if !info.IsDir() {
		return errors.New("not a directory")
	}
	tw := tar.NewWriter(w)
	if err := referenceWalkInto(tw, root, root); err != nil {
		_ = tw.Close()
		return err
	}
	return tw.Close()
}

func referenceWalkInto(tw *tar.Writer, root, dir string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if dir == root && strings.HasPrefix(entry.Name(), scratchformat.GenerationMarkerFile) {
			continue
		}
		full := filepath.Join(dir, entry.Name())
		rel, err := filepath.Rel(root, full)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)

		info, err := entry.Info()
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			continue
		}
		if entry.IsDir() {
			if err := tw.WriteHeader(&tar.Header{
				Name:     rel + "/",
				Typeflag: tar.TypeDir,
				Mode:     int64(info.Mode().Perm()),
			}); err != nil {
				return err
			}
			if err := referenceWalkInto(tw, root, full); err != nil {
				return err
			}
			continue
		}
		if !info.Mode().IsRegular() {
			continue
		}
		f, err := os.Open(full)
		if err != nil {
			return err
		}
		size := info.Size()
		if err := tw.WriteHeader(&tar.Header{
			Name:     rel,
			Typeflag: tar.TypeReg,
			Mode:     int64(info.Mode().Perm()),
			Size:     size,
		}); err != nil {
			_ = f.Close()
			return err
		}
		_, err = io.Copy(tw, &io.LimitedReader{R: f, N: size})
		_ = f.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

// representativeWorkingSet builds a tree with every kind of entry a working set
// has been seen to hold, and the ones the walk must leave out.
func representativeWorkingSet(t *testing.T) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), "work")

	// Larger than one copy buffer, and not a multiple of the tar block size.
	region := make([]byte, 100_000+7)
	for i := range region {
		region[i] = byte(i*31 + i>>8)
	}
	deep := strings.Repeat("d/", 12)
	long := strings.Repeat("long-name-", 15) // 150 bytes: needs a PAX record
	writeTree(t, root, map[string]string{
		"server.properties":            "server-port=25565\n",
		"eula.txt":                     "eula=true\n",
		"empty.txt":                    "",
		"run.sh":                       "#!/bin/sh\n",
		"ops.json":                     "[]",
		"name with space.txt":          "space",
		"ワールド/設定.yml":                  "unicode",
		long + ".txt":                  "long",
		"world/level.dat":              "level",
		"world/region/r.0.0.mca":       string(region),
		"world/region/r.0.1.mca":       strings.Repeat("x", 512),
		"world/.mcsd_generation":       "kept: only the root-level marker is private",
		"plugins/a/config.yml":         "a",
		"plugins/b/config.yml":         "b",
		"logs/latest.log":              "log\n",
		deep + "leaf.txt":              "deep",
		".mcsd_generation":             "7",
		".mcsd_generation-1234567":     "7",
		"mods/linked-a.jar":            "hard-linked content",
		"libraries/net/x/1.0/args.txt": "args",
	})
	mkdir := func(rel string, mode os.FileMode) {
		t.Helper()
		if err := os.MkdirAll(filepath.Join(root, rel), 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(filepath.Join(root, rel), mode); err != nil {
			t.Fatal(err)
		}
	}
	chmod := func(rel string, mode os.FileMode) {
		t.Helper()
		if err := os.Chmod(filepath.Join(root, rel), mode); err != nil {
			t.Fatal(err)
		}
	}
	mkdir("empty-dir", 0o700)
	mkdir("world/playerdata", 0o755)
	mkdir("shared", 0o777|os.ModeSticky)
	chmod("run.sh", 0o755)
	chmod("eula.txt", 0o644)
	chmod("ops.json", 0o600)
	chmod("world/level.dat", 0o755|os.ModeSetuid|os.ModeSetgid)
	chmod("plugins", 0o755)

	// Two names for one file: each is archived as its own regular member.
	if err := os.Link(filepath.Join(root, "mods/linked-a.jar"), filepath.Join(root, "mods/linked-b.jar")); err != nil {
		t.Fatal(err)
	}
	// Left out of the archive: links (to a file, to a directory, to nothing) and
	// special files.
	for name, target := range map[string]string{
		"link-to-file":      "server.properties",
		"link-to-dir":       "world",
		"link-to-nothing":   "absent",
		"world/link-to-etc": "/etc",
	} {
		if err := os.Symlink(target, filepath.Join(root, name)); err != nil {
			t.Fatal(err)
		}
	}
	if err := syscall.Mkfifo(filepath.Join(root, "logs/pipe"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mknod(filepath.Join(root, "plugins/a/sock"), syscall.S_IFSOCK|0o640, 0); err != nil {
		t.Fatal(err)
	}
	return root
}

// The archive of a working set nobody is changing is byte-for-byte what the
// path-based walk produced: same members in the same order, same headers.
func TestPackOfAQuiescentWorkingSetIsByteIdenticalToThePathBasedWalk(t *testing.T) {
	root := representativeWorkingSet(t)

	var want, got bytes.Buffer
	if err := referencePackTar(root, &want); err != nil {
		t.Fatalf("reference pack: %v", err)
	}
	h := &capturingHandler{}
	if err := packTar(root, &got, slog.New(h)); err != nil {
		t.Fatalf("packTar: %v", err)
	}

	if !bytes.Equal(got.Bytes(), want.Bytes()) {
		t.Fatalf("archive differs from the path-based walk: got %d bytes, want %d\n got members: %v\nwant members: %v",
			got.Len(), want.Len(), memberNames(t, got.Bytes()), memberNames(t, want.Bytes()))
	}
	if len(h.records) != 0 {
		t.Errorf("a quiescent working set must pack without a log line, got: %v", h.records)
	}

	// Guard the fixture itself: the comparison is only worth something if the
	// archive holds what the tree was built to exercise.
	names := memberNames(t, got.Bytes())
	for _, must := range []string{"empty-dir/", "world/region/r.0.0.mca", "mods/linked-a.jar", "mods/linked-b.jar", "world/.mcsd_generation", "ワールド/設定.yml"} {
		if !contains(names, must) {
			t.Errorf("fixture member %q missing from the archive: %v", must, names)
		}
	}
	for _, mustNot := range []string{".mcsd_generation", ".mcsd_generation-1234567", "link-to-file", "link-to-dir/", "world/link-to-etc/", "logs/pipe", "plugins/a/sock"} {
		if contains(names, mustNot) {
			t.Errorf("%q must not be archived: %v", mustNot, names)
		}
	}
}

func memberNames(t *testing.T, archive []byte) []string {
	t.Helper()
	var names []string
	tr := tar.NewReader(bytes.NewReader(archive))
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return names
		}
		if err != nil {
			t.Fatalf("tar.Next: %v", err)
		}
		names = append(names, hdr.Name)
	}
}

func contains(names []string, name string) bool {
	for _, n := range names {
		if n == name {
			return true
		}
	}
	return false
}
