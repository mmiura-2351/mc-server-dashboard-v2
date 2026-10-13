package containerdriver

import (
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

// When the Worker cannot give what it just created to the working set's owner (a
// root Worker without CAP_CHOWN), the log is refused — and what that open created
// must not stay behind as the Worker's: a root-owned 0750 logs/ is one the
// unprivileged server cannot write its own logs into (issue #3338).

// failInstallLogChown makes the ownership inheritance fail with EPERM for
// directories, for files, or for both, running hook (if any) first — at the
// moment the entry exists and is still the Worker's.
func failInstallLogChown(t *testing.T, failDir, failFile bool, hook func()) {
	t.Helper()
	prev := installLogFchown
	installLogFchown = func(fd, _, _ int) error {
		var st unix.Stat_t
		if err := unix.Fstat(fd, &st); err != nil {
			return err
		}
		if isDir := st.Mode&unix.S_IFMT == unix.S_IFDIR; (isDir && !failDir) || (!isDir && !failFile) {
			return nil
		}
		if hook != nil {
			hook()
		}
		return unix.EPERM
	}
	t.Cleanup(func() { installLogFchown = prev })
}

// A logs/ the open created itself is removed again when it cannot be handed to
// the working set's owner; the refusal and its warning stay.
func TestInstallLogRemovesTheDirectoryItCreatedWhenItCannotBeHandedOver(t *testing.T) {
	dir := t.TempDir()
	failInstallLogChown(t, true, true, nil)

	_, logged := captureInstallLog(t, dir, installLogOutput)

	assertRefused(t, logged)
	assertEmptyDir(t, dir)
}

// The same for the log file: it is removed, and the logs/ it was created in —
// the server's own, or one this open created and did hand over — stays.
func TestInstallLogRemovesTheLogItCreatedWhenItCannotBeHandedOver(t *testing.T) {
	for _, tc := range []struct {
		name     string
		existing bool
	}{
		{"in a logs/ that already existed", true},
		{"in a logs/ this open created", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			if tc.existing {
				plantAtLogPath(t, dir)
			}
			failInstallLogChown(t, false, true, nil)

			_, logged := captureInstallLog(t, dir, installLogOutput)

			assertRefused(t, logged)
			assertEmptyDir(t, filepath.Join(dir, "logs"))
		})
	}
}

// Only what the open created is removed. The installer is running meanwhile and
// may have put something of its own in the place of the new entry, or inside it;
// that is the server's and stays.
func TestInstallLogCleanupLeavesWhatItDidNotCreate(t *testing.T) {
	sameFile := func(t *testing.T, path string, want os.FileInfo) {
		t.Helper()
		got, err := os.Lstat(path)
		if err != nil {
			t.Fatalf("%s is gone, want it left in place: %v", path, err)
		}
		if !os.SameFile(got, want) {
			t.Fatalf("%s is not the entry the installer put there", path)
		}
	}

	t.Run("logs/ replaced by another directory", func(t *testing.T) {
		dir := t.TempDir()
		logs := filepath.Join(dir, "logs")
		var planted os.FileInfo
		failInstallLogChown(t, true, true, func() {
			if err := os.Rename(logs, logs+".moved"); err != nil {
				t.Error(err)
			}
			if err := os.Mkdir(logs, 0o750); err != nil {
				t.Error(err)
			}
			planted, _ = os.Lstat(logs)
		})

		_, logged := captureInstallLog(t, dir, installLogOutput)

		assertRefused(t, logged)
		sameFile(t, logs, planted)
	})

	t.Run("logs/ no longer empty", func(t *testing.T) {
		dir := t.TempDir()
		logs := filepath.Join(dir, "logs")
		failInstallLogChown(t, true, true, func() {
			if err := os.WriteFile(filepath.Join(logs, "latest.log"), []byte("x"), 0o600); err != nil {
				t.Error(err)
			}
		})

		_, logged := captureInstallLog(t, dir, installLogOutput)

		assertRefused(t, logged)
		if _, err := os.Stat(filepath.Join(logs, "latest.log")); err != nil {
			t.Fatalf("the server's file in logs/ is gone: %v", err)
		}
	})

	t.Run("the log no longer empty", func(t *testing.T) {
		dir := t.TempDir()
		logPath := plantAtLogPath(t, dir)
		failInstallLogChown(t, false, true, func() {
			if err := os.WriteFile(logPath, []byte("x"), 0o600); err != nil {
				t.Error(err)
			}
		})

		_, logged := captureInstallLog(t, dir, installLogOutput)

		assertRefused(t, logged)
		if got := readInstallLog(t, dir); got != "x" {
			t.Fatalf("install log = %q, want what was written to it left in place", got)
		}
	})

	t.Run("the log replaced by another file", func(t *testing.T) {
		dir := t.TempDir()
		logPath := plantAtLogPath(t, dir)
		var planted os.FileInfo
		failInstallLogChown(t, false, true, func() {
			if err := os.Rename(logPath, logPath+".moved"); err != nil {
				t.Error(err)
			}
			if err := os.WriteFile(logPath, nil, 0o600); err != nil {
				t.Error(err)
			}
			planted, _ = os.Lstat(logPath)
		})

		_, logged := captureInstallLog(t, dir, installLogOutput)

		assertRefused(t, logged)
		sameFile(t, logPath, planted)
	})
}

// A logs/ and a log that already existed are never re-owned, so there is nothing
// to fail and nothing to remove: the capture appends as before.
func TestInstallLogNeverRemovesAnExistingLogsDir(t *testing.T) {
	dir := t.TempDir()
	captureInstallLog(t, dir, "first attempt\n")
	failInstallLogChown(t, true, true, nil)

	_, logged := captureInstallLog(t, dir, "second attempt\n")

	if logged != "" {
		t.Fatalf("worker log = %q, want nothing logged", logged)
	}
	if got, want := readInstallLog(t, dir), "first attempt\nsecond attempt\n"; got != want {
		t.Fatalf("install log = %q, want %q", got, want)
	}
}
