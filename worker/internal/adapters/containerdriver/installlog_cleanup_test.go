package containerdriver

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

// When the Worker cannot give what it creates to the working set's owner (a root
// Worker without CAP_CHOWN), the log is refused — and nothing of the Worker's may
// stay behind: a root-owned 0750 logs/ is one the unprivileged server cannot
// write its own logs into. Nor may anything be removed to get there: the
// installer is running and can replace any entry at any moment, so a removal by
// name could land on its entry whatever was checked first. These tests fail the
// hand-over at each step and let the installer act at the worst moments (issue
// #3338).

// failInstallLogChown makes the ownership inheritance fail with EPERM whenever
// fail says so — it is told whether the entry is a "dir" or a "file" and how
// many entries of that kind came before — after running hook (if any): the
// moment the Worker has learnt the entry cannot be handed over, past every
// check it makes.
func failInstallLogChown(t *testing.T, fail func(kind string, nth int) bool, hook func()) {
	t.Helper()
	prev := installLogFchown
	seen := map[string]int{}
	installLogFchown = func(fd, _, _ int) error {
		var st unix.Stat_t
		if err := unix.Fstat(fd, &st); err != nil {
			return err
		}
		kind := "file"
		if st.Mode&unix.S_IFMT == unix.S_IFDIR {
			kind = "dir"
		}
		nth := seen[kind]
		seen[kind]++
		if !fail(kind, nth) {
			return nil
		}
		if hook != nil {
			hook()
		}
		return unix.EPERM
	}
	t.Cleanup(func() { installLogFchown = prev })
}

func always(string, int) bool { return true }

// withoutUnnamedFiles stands in for a filesystem that cannot create a file with
// no name (NFS, some FUSE filesystems).
func withoutUnnamedFiles(t *testing.T) {
	t.Helper()
	prev := installLogOpenUnnamed
	installLogOpenUnnamed = func(int) (int, error) { return -1, unix.EOPNOTSUPP }
	t.Cleanup(func() { installLogOpenUnnamed = prev })
}

// assertEntries fails unless dir holds exactly the named entries.
func assertEntries(t *testing.T, dir string, want ...string) {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, e := range entries {
		got = append(got, e.Name())
	}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("%s holds %v, want %v", dir, got, want)
	}
}

func assertFileReads(t *testing.T, path, want string) {
	t.Helper()
	got, err := os.ReadFile(path)
	if err != nil || string(got) != want {
		t.Fatalf("%s reads %q (%v), want %q", path, got, err, want)
	}
}

// Without the means to hand anything over, no logs/ is created at all.
func TestInstallLogCreatesNoDirectoryItCannotHandOver(t *testing.T) {
	dir := t.TempDir()
	failInstallLogChown(t, always, nil)

	_, logged := captureInstallLog(t, dir, installLogOutput)

	assertRefused(t, logged)
	assertEmptyDir(t, dir)
}

// A log that cannot be handed over never gets a name, in a logs/ that already
// existed or in one this open created and did hand over.
func TestInstallLogCreatesNoLogItCannotHandOver(t *testing.T) {
	t.Run("in a logs/ that already existed", func(t *testing.T) {
		dir := t.TempDir()
		plantAtLogPath(t, dir)
		failInstallLogChown(t, always, nil)

		_, logged := captureInstallLog(t, dir, installLogOutput)

		assertRefused(t, logged)
		assertEmptyDir(t, filepath.Join(dir, "logs"))
	})

	t.Run("in a logs/ this open created", func(t *testing.T) {
		dir := t.TempDir()
		// The first file is the one the directory's hand-over is tried on; the
		// second is the log.
		failInstallLogChown(t, func(kind string, nth int) bool { return kind == "file" && nth == 1 }, nil)

		_, logged := captureInstallLog(t, dir, installLogOutput)

		assertRefused(t, logged)
		assertEmptyDir(t, filepath.Join(dir, "logs"))
	})
}

// The installer puts its own entry where the Worker's would have been, at the
// very moment the Worker learns it cannot hand its own over — after every check
// there is. Nothing is removed, so the installer's entry is intact.
func TestInstallLogRefusalLeavesWhatTheInstallerCreatesMeanwhile(t *testing.T) {
	t.Run("its own log", func(t *testing.T) {
		dir := t.TempDir()
		logPath := plantAtLogPath(t, dir)
		failInstallLogChown(t, always, func() {
			if err := os.WriteFile(logPath, []byte("the installer's"), 0o600); err != nil {
				t.Error(err)
			}
		})

		_, logged := captureInstallLog(t, dir, installLogOutput)

		assertRefused(t, logged)
		assertFileReads(t, logPath, "the installer's")
	})

	t.Run("its own logs/", func(t *testing.T) {
		dir := t.TempDir()
		latest := filepath.Join(dir, "logs", "latest.log")
		failInstallLogChown(t, always, func() {
			if err := os.Mkdir(filepath.Dir(latest), 0o750); err != nil {
				t.Error(err)
			}
			if err := os.WriteFile(latest, []byte("the installer's"), 0o600); err != nil {
				t.Error(err)
			}
		})

		_, logged := captureInstallLog(t, dir, installLogOutput)

		assertRefused(t, logged)
		assertEntries(t, filepath.Join(dir, "logs"), "latest.log")
		assertFileReads(t, latest, "the installer's")
	})
}

// replaceLogsDirAfterMkdir has the installer move the logs/ the Worker just made
// aside and put a directory of its own, holding a file, in its place — between
// the Worker's mkdir and its open of the result.
func replaceLogsDirAfterMkdir(t *testing.T, dir string) {
	t.Helper()
	logs := filepath.Join(dir, "logs")
	prev := installLogAfterMkdir
	installLogAfterMkdir = func() {
		if err := os.Rename(logs, logs+".moved"); err != nil {
			t.Error(err)
		}
		if err := os.Mkdir(logs, 0o750); err != nil {
			t.Error(err)
		}
		if err := os.WriteFile(filepath.Join(logs, "latest.log"), []byte("the installer's"), 0o600); err != nil {
			t.Error(err)
		}
	}
	t.Cleanup(func() { installLogAfterMkdir = prev })
}

// A logs/ replaced between the Worker's mkdir and its open is the installer's
// own directory inside the working set: the log goes into it, and neither it nor
// the one moved aside is removed.
func TestInstallLogUsesADirectoryReplacedBetweenMkdirAndOpen(t *testing.T) {
	dir := t.TempDir()
	replaceLogsDirAfterMkdir(t, dir)

	_, logged := captureInstallLog(t, dir, installLogOutput)

	if logged != "" {
		t.Fatalf("worker log = %q, want nothing logged", logged)
	}
	assertEntries(t, dir, "logs", "logs.moved")
	assertEntries(t, filepath.Join(dir, "logs"), "forge-install.log", "latest.log")
	assertFileReads(t, filepath.Join(dir, "logs", "latest.log"), "the installer's")
	if got := readInstallLog(t, dir); got != installLogOutput {
		t.Fatalf("install log = %q, want %q", got, installLogOutput)
	}
}

// The same replacement when the directory then cannot be handed over: the open
// is refused and still nothing is removed — not the installer's directory, which
// now has the name, and not the one moved aside.
func TestInstallLogRefusalRemovesNothingAfterADirectoryReplacement(t *testing.T) {
	dir := t.TempDir()
	replaceLogsDirAfterMkdir(t, dir)
	failInstallLogChown(t, func(kind string, _ int) bool { return kind == "dir" }, nil)

	_, logged := captureInstallLog(t, dir, installLogOutput)

	assertRefused(t, logged)
	assertEntries(t, dir, "logs", "logs.moved")
	assertEntries(t, filepath.Join(dir, "logs"), "latest.log")
}

// A log the installer creates after the Worker found none, and before the
// Worker's own gets its name, is not replaced: the Worker's link fails on it and
// the output is appended to the installer's file, like to any existing log.
func TestInstallLogAppendsToALogCreatedBeforeItsOwnIsNamed(t *testing.T) {
	dir := t.TempDir()
	logPath := plantAtLogPath(t, dir)
	prev := installLogFchown
	installLogFchown = func(int, int, int) error {
		return os.WriteFile(logPath, []byte("the installer's\n"), 0o600)
	}
	t.Cleanup(func() { installLogFchown = prev })

	_, logged := captureInstallLog(t, dir, installLogOutput)

	if logged != "" {
		t.Fatalf("worker log = %q, want nothing logged", logged)
	}
	assertEntries(t, filepath.Join(dir, "logs"), "forge-install.log")
	if got, want := readInstallLog(t, dir), "the installer's\n"+installLogOutput; got != want {
		t.Fatalf("install log = %q, want %q", got, want)
	}
}

// A logs/ the installer creates after the Worker found none, and before the
// Worker makes its own, is used as it is: it is the installer's, and is not
// re-owned.
func TestInstallLogUsesADirectoryCreatedBeforeItsOwn(t *testing.T) {
	dir := t.TempDir()
	prev := installLogFchown
	var dirsReowned int
	installLogFchown = func(fd, _, _ int) error {
		var st unix.Stat_t
		if err := unix.Fstat(fd, &st); err != nil {
			return err
		}
		if st.Mode&unix.S_IFMT == unix.S_IFDIR {
			dirsReowned++
		}
		// Idempotent: the first call is the one made before logs/ is created.
		if err := os.Mkdir(filepath.Join(dir, "logs"), 0o750); err != nil && !os.IsExist(err) {
			return err
		}
		return nil
	}
	t.Cleanup(func() { installLogFchown = prev })

	_, logged := captureInstallLog(t, dir, installLogOutput)

	if got := readInstallLog(t, dir); got != installLogOutput || logged != "" {
		t.Fatalf("install log = %q, worker log = %q; want the output written and nothing logged", got, logged)
	}
	if dirsReowned != 0 {
		t.Fatalf("re-owned %d directories, want the installer's logs/ left as it is", dirsReowned)
	}
}

// On a filesystem without unnamed files the entries have their names before they
// can be handed over. The log is still written there.
func TestInstallLogIsWrittenWithoutUnnamedFiles(t *testing.T) {
	dir := t.TempDir()
	withoutUnnamedFiles(t)
	calls := swapInstallLogOwner(t, 7, 7)

	_, logged := captureInstallLog(t, dir, installLogOutput)

	if got := readInstallLog(t, dir); got != installLogOutput || logged != "" {
		t.Fatalf("install log = %q, worker log = %q; want the output written and nothing logged", got, logged)
	}
	if got, want := strings.Join(*calls, ", "), "dir 7:7, file 7:7"; got != want {
		t.Fatalf("owner changes on create = %q, want %q", got, want)
	}
}

// There, an entry that cannot be handed over is left in place and reported as
// left — never removed, so what the installer put under its name in the meantime
// is never removed either.
func TestInstallLogWithoutUnnamedFilesLeavesAndReportsWhatItCannotHandOver(t *testing.T) {
	assertLeft := func(t *testing.T, logged string) {
		t.Helper()
		assertRefused(t, logged)
		if !strings.Contains(logged, "left in place") {
			t.Fatalf("worker log = %q, want it to say the entry was left in place", logged)
		}
	}

	t.Run("the directory", func(t *testing.T) {
		dir := t.TempDir()
		withoutUnnamedFiles(t)
		logs := filepath.Join(dir, "logs")
		failInstallLogChown(t, always, func() {
			if err := os.Rename(logs, logs+".moved"); err != nil {
				t.Error(err)
			}
			if err := os.Mkdir(logs, 0o750); err != nil {
				t.Error(err)
			}
		})

		_, logged := captureInstallLog(t, dir, installLogOutput)

		assertLeft(t, logged)
		assertEntries(t, dir, "logs", "logs.moved")
	})

	t.Run("the log", func(t *testing.T) {
		dir := t.TempDir()
		withoutUnnamedFiles(t)
		logPath := plantAtLogPath(t, dir)
		failInstallLogChown(t, always, func() {
			if err := os.Rename(logPath, logPath+".moved"); err != nil {
				t.Error(err)
			}
			if err := os.WriteFile(logPath, []byte("the installer's"), 0o600); err != nil {
				t.Error(err)
			}
		})

		_, logged := captureInstallLog(t, dir, installLogOutput)

		assertLeft(t, logged)
		assertEntries(t, filepath.Join(dir, "logs"), "forge-install.log", "forge-install.log.moved")
		assertFileReads(t, logPath, "the installer's")
	})
}

// A logs/ and a log that already existed are never re-owned, so there is nothing
// to fail: the capture appends as before.
func TestInstallLogAppendsToAnExistingLogItCouldNotHandOver(t *testing.T) {
	dir := t.TempDir()
	captureInstallLog(t, dir, "first attempt\n")
	failInstallLogChown(t, always, nil)

	_, logged := captureInstallLog(t, dir, "second attempt\n")

	if logged != "" {
		t.Fatalf("worker log = %q, want nothing logged", logged)
	}
	if got, want := readInstallLog(t, dir), "first attempt\nsecond attempt\n"; got != want {
		t.Fatalf("install log = %q, want %q", got, want)
	}
}

// A created log is a regular file with one name and the mode the named create
// gave it, whichever way it got its name.
func TestInstallLogCreatedUnnamedIsAnOrdinaryFile(t *testing.T) {
	dir := t.TempDir()

	captureInstallLog(t, dir, installLogOutput)

	var st unix.Stat_t
	if err := unix.Lstat(filepath.Join(dir, "logs", "forge-install.log"), &st); err != nil {
		t.Fatal(err)
	}
	var mask unix.Stat_t
	probe := filepath.Join(dir, "probe")
	if err := os.WriteFile(probe, nil, 0o640); err != nil {
		t.Fatal(err)
	}
	if err := unix.Lstat(probe, &mask); err != nil {
		t.Fatal(err)
	}
	if st.Mode != mask.Mode || st.Nlink != 1 {
		t.Fatalf("install log has mode %o and %d names, want %o and 1", st.Mode, st.Nlink, mask.Mode)
	}
}
