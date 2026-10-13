package containerdriver

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// The install log is written by the Worker (root, in the shipped topology) into
// a working set the installer — code the operator does not control — is writing
// to at the same moment. These tests plant what such an installer could plant at
// logs/ and logs/forge-install.log and pin that the capture never leaves the
// working set (issue #3298).

const installLogOutput = "downloading libraries\n"

// captureInstallLog runs one install-output capture against workingDir with the
// installer printing output, and returns the scan and what the Worker logged.
func captureInstallLog(t *testing.T, workingDir, output string) (*installOutputScan, string) {
	t.Helper()
	docker := newForgeFakeDocker()
	docker.logBodies[forgeInstallID] = string(frame(dockerStreamStdout, output))
	logged := &syncBuffer{}
	i := &instance{
		docker: docker,
		spec:   forgeSpec(workingDir),
		logger: slog.New(slog.NewTextHandler(logged, nil)),
	}
	return i.captureInstallOutput(forgeInstallID), logged.String()
}

// assertRefused fails unless the capture reported that it wrote no log.
func assertRefused(t *testing.T, logged string) {
	t.Helper()
	if !strings.Contains(logged, "level=WARN") || !strings.Contains(logged, "forge install log not written") {
		t.Fatalf("worker log = %q, want a warning that the install log was not written", logged)
	}
}

// assertEmptyDir fails if anything was created in dir.
func assertEmptyDir(t *testing.T, dir string) {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("%s holds %v, want nothing created outside the working set", dir, entries)
	}
}

func readInstallLog(t *testing.T, workingDir string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(workingDir, filepath.FromSlash(execution.ForgeInstallLogRelpath)))
	if err != nil {
		t.Fatalf("read install log: %v", err)
	}
	return string(data)
}

// The log and its directory are created when absent, and the output lands in it.
func TestInstallLogCreatedInsideWorkingSet(t *testing.T) {
	dir := t.TempDir()

	_, logged := captureInstallLog(t, dir, installLogOutput)

	if got := readInstallLog(t, dir); got != installLogOutput {
		t.Fatalf("install log = %q, want %q", got, installLogOutput)
	}
	if logged != "" {
		t.Fatalf("worker log = %q, want nothing logged for a written install log", logged)
	}
}

// The log is appended to across attempts and starts, never truncated.
func TestInstallLogAppendsAcrossRuns(t *testing.T) {
	dir := t.TempDir()

	captureInstallLog(t, dir, "first attempt\n")
	captureInstallLog(t, dir, "second attempt\n")

	if got, want := readInstallLog(t, dir), "first attempt\nsecond attempt\n"; got != want {
		t.Fatalf("install log = %q, want %q", got, want)
	}
}

// logs/ swapped for a symlink to a directory outside the working set: nothing is
// created behind the link.
func TestInstallLogRefusesSymlinkedLogsDir(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	if err := os.Symlink(outside, filepath.Join(dir, "logs")); err != nil {
		t.Fatal(err)
	}

	_, logged := captureInstallLog(t, dir, installLogOutput)

	assertEmptyDir(t, outside)
	assertRefused(t, logged)
}

// logs/ as a dangling symlink: the directory it names is not created.
func TestInstallLogRefusesDanglingLogsDirSymlink(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	if err := os.Symlink(filepath.Join(outside, "absent"), filepath.Join(dir, "logs")); err != nil {
		t.Fatal(err)
	}

	_, logged := captureInstallLog(t, dir, installLogOutput)

	assertEmptyDir(t, outside)
	assertRefused(t, logged)
}

// plantAtLogPath creates a real logs/ in dir and returns the log's path, for a
// test to plant something at.
func plantAtLogPath(t *testing.T, dir string) string {
	t.Helper()
	logPath := filepath.Join(dir, filepath.FromSlash(execution.ForgeInstallLogRelpath))
	if err := os.MkdirAll(filepath.Dir(logPath), 0o750); err != nil {
		t.Fatal(err)
	}
	return logPath
}

// The log file swapped for a symlink to a file outside the working set: that
// file is not appended to.
func TestInstallLogRefusesSymlinkedLogFile(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	victim := filepath.Join(outside, "victim")
	if err := os.WriteFile(victim, []byte("untouched"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(victim, plantAtLogPath(t, dir)); err != nil {
		t.Fatal(err)
	}

	_, logged := captureInstallLog(t, dir, installLogOutput)

	if got, err := os.ReadFile(victim); err != nil || string(got) != "untouched" {
		t.Fatalf("file outside the working set = %q (err %v), want it untouched", got, err)
	}
	assertRefused(t, logged)
}

// The log file as a dangling symlink: the file it names is not created.
func TestInstallLogRefusesDanglingLogFileSymlink(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	if err := os.Symlink(filepath.Join(outside, "absent"), plantAtLogPath(t, dir)); err != nil {
		t.Fatal(err)
	}

	_, logged := captureInstallLog(t, dir, installLogOutput)

	assertEmptyDir(t, outside)
	assertRefused(t, logged)
}

// The log path as a second name of a file kept elsewhere: an append through it
// would land in that file too, so it is refused.
func TestInstallLogRefusesHardLinkedLogFile(t *testing.T) {
	dir := t.TempDir()
	victim := filepath.Join(dir, "victim")
	if err := os.WriteFile(victim, []byte("untouched"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(victim, plantAtLogPath(t, dir)); err != nil {
		t.Fatal(err)
	}

	_, logged := captureInstallLog(t, dir, installLogOutput)

	if got, err := os.ReadFile(victim); err != nil || string(got) != "untouched" {
		t.Fatalf("hard-linked file = %q (err %v), want it untouched", got, err)
	}
	assertRefused(t, logged)
}

// A FIFO at the log path is refused without blocking the supervisor: with nobody
// reading it the open itself must not wait for a reader, and with a reader
// holding it open nothing is fed to that reader.
func TestInstallLogRefusesFIFOAtLogPath(t *testing.T) {
	for _, withReader := range []bool{false, true} {
		name := "no reader"
		if withReader {
			name = "reader attached"
		}
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			logPath := plantAtLogPath(t, dir)
			if err := unix.Mkfifo(logPath, 0o600); err != nil {
				t.Fatal(err)
			}
			var reader *os.File
			if withReader {
				var err error
				reader, err = os.OpenFile(logPath, os.O_RDONLY|unix.O_NONBLOCK, 0)
				if err != nil {
					t.Fatal(err)
				}
				defer func() { _ = reader.Close() }()
			}

			done := make(chan string, 1)
			go func() {
				_, logged := captureInstallLog(t, dir, installLogOutput)
				done <- logged
			}()
			select {
			case logged := <-done:
				assertRefused(t, logged)
			case <-time.After(5 * time.Second):
				t.Fatal("capture blocked on a FIFO at the install log path")
			}
			if withReader {
				buf := make([]byte, 64)
				if n, _ := reader.Read(buf); n != 0 {
					t.Fatalf("FIFO reader received %q, want nothing written to it", buf[:n])
				}
			}
		})
	}
}

// A directory at the log path is refused like any other non-file.
func TestInstallLogRefusesDirectoryAtLogPath(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(plantAtLogPath(t, dir), 0o750); err != nil {
		t.Fatal(err)
	}

	_, logged := captureInstallLog(t, dir, installLogOutput)

	assertRefused(t, logged)
}

// A refused log does not cost the failure diagnosis: the stream is still scanned,
// so an installer cannot hide an out-of-memory failure by blocking the log.
func TestInstallLogRefusalStillScansOutput(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(plantAtLogPath(t, dir), 0o750); err != nil {
		t.Fatal(err)
	}

	scan, _ := captureInstallLog(t, dir, "java.lang.OutOfMemoryError: Java heap space\n")

	if !scan.outOfMemory {
		t.Fatal("scan missed the out-of-memory marker when the install log was refused")
	}
}

// swapInstallLogOwner replaces the two descriptor calls that give a created
// entry its directory's owner, so a test can make that owner someone other than
// the test process (only root can give a file away). It returns the recorded
// fchown calls as "<mode-type> uid:gid".
func swapInstallLogOwner(t *testing.T, uid, gid int) *[]string {
	t.Helper()
	prevOwner, prevChown := installLogDirOwner, installLogFchown
	var calls []string
	installLogDirOwner = func(int) (int, int, error) { return uid, gid, nil }
	installLogFchown = func(fd, u, g int) error {
		var st unix.Stat_t
		if err := unix.Fstat(fd, &st); err != nil {
			return err
		}
		kind := "file"
		if st.Mode&unix.S_IFMT == unix.S_IFDIR {
			kind = "dir"
		}
		calls = append(calls, fmt.Sprintf("%s %d:%d", kind, u, g))
		return nil
	}
	t.Cleanup(func() { installLogDirOwner, installLogFchown = prevOwner, prevChown })
	return &calls
}

// What the Worker creates takes the owner of the directory it is created in — the
// run-as user once the working set is handed over — so the unprivileged server
// can still use its own logs/ (issue #2600). What already existed is never
// re-owned: it may be the installer's plant.
func TestInstallLogCreatedEntriesTakeTheDirectoryOwner(t *testing.T) {
	dir := t.TempDir()
	calls := swapInstallLogOwner(t, 7, 7)

	captureInstallLog(t, dir, "first attempt\n")
	// The leading file is the unnamed one the hand-over is tried on before logs/
	// is created (issue #3338).
	if got, want := strings.Join(*calls, ", "), "file 7:7, dir 7:7, file 7:7"; got != want {
		t.Fatalf("owner changes on create = %q, want %q", got, want)
	}

	*calls = nil
	captureInstallLog(t, dir, "second attempt\n")
	if len(*calls) != 0 {
		t.Fatalf("owner changes on an existing log = %v, want none", *calls)
	}
}

// End to end: with logs/ planted as a link out of the working set, the install is
// still supervised to a running server, with no log written anywhere.
func TestForgeContainerInstallProceedsWithoutARefusedLog(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	if err := os.Symlink(outside, filepath.Join(dir, "logs")); err != nil {
		t.Fatal(err)
	}
	docker := newForgeFakeDocker()
	docker.logBodies[forgeInstallID] = string(frame(dockerStreamStdout, installLogOutput))
	logged := &syncBuffer{}
	d := forgeDriver(docker)
	d.logger = slog.New(slog.NewTextHandler(logged, nil))

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	writeArgsfile(t, dir)
	docker.exit(forgeInstallID, 0, nil)
	drainTo(t, inst.Events(), execution.StateRunning)

	assertEmptyDir(t, outside)
	assertRefused(t, logged.String())
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}
