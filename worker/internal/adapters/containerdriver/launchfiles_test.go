package containerdriver

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// The launch planning runs as the Worker (root, in the shipped topology) over a
// working set that server-controlled code and the Forge installer can write,
// including while the planning runs. These tests plant what such code could plant
// and pin that no launch decision — and no name on a command line — comes from
// anything outside the working set (issue #3352).

const (
	// outsideVersion names a directory that exists only outside the working set.
	outsideVersion = "9.9.9-outside"
	userJVMArgs    = "user_jvm_args.txt"
)

var (
	wantInstallArgs = []string{"-jar", "/data/server.jar", "--installServer"}
	wantInstallCmd  = append([]string{"java", "-Duser.home=/tmp"}, wantInstallArgs...)
	wantForgeLaunch = []string{"@/data/" + forgeArgsRel, "nogui"}
)

// planForge plans the Forge launch of the working set at dir through the
// driver's resolver, returning the plan, what the resolver logged and the error.
func planForge(t *testing.T, dir string) (execution.LaunchPlan, string, error) {
	t.Helper()
	logged := &syncBuffer{}
	logger := slog.New(slog.NewTextHandler(logged, nil)).With("server_id", "s1")
	plan, err := execution.BuildLaunchPlan(forgeSpec(dir), containerPathResolver(dir, logger))
	return plan, logged.String(), err
}

// assertNotInstalled fails unless the plan is exactly the installer step: a
// tampered layout must read as "Forge is not installed", with nothing else on the
// command line.
func assertNotInstalled(t *testing.T, plan execution.LaunchPlan, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("plan err = %v, want nil", err)
	}
	want := execution.LaunchPlan{NeedsInstall: true, InstallArgs: wantInstallArgs}
	if !reflect.DeepEqual(plan, want) {
		t.Fatalf("plan = %+v, want the installer step %+v", plan, want)
	}
}

// assertForgeLaunch fails unless the plan launches through the working set's own
// args file and nothing else.
func assertForgeLaunch(t *testing.T, plan execution.LaunchPlan, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("plan err = %v, want nil", err)
	}
	want := execution.LaunchPlan{LaunchArgs: wantForgeLaunch}
	if !reflect.DeepEqual(plan, want) {
		t.Fatalf("plan = %+v, want %+v", plan, want)
	}
}

func symlink(t *testing.T, target, link string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(link), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
}

func mkdir(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(path, 0o750); err != nil {
		t.Fatal(err)
	}
}

func mkfifo(t *testing.T, path string) {
	t.Helper()
	mkdir(t, filepath.Dir(path))
	if err := unix.Mkfifo(path, 0o600); err != nil {
		t.Fatal(err)
	}
}

// Any component of the args-file path planted as a link to an outside directory
// that holds a matching args file reads as "not installed": the outside entries
// decide nothing and the outside directory name reaches no command line.
func TestLaunchPlanNeverFollowsASymlinkedComponent(t *testing.T) {
	components := []string{
		"libraries",
		"libraries/net",
		"libraries/net/minecraftforge",
		forgeVersionsRel,
		forgeVersionsRel + "/1.20.1-47.2.0",
	}
	for _, linked := range components {
		t.Run(linked, func(t *testing.T) {
			dir, outside := t.TempDir(), t.TempDir()
			// The outside tree holds whatever the path below the link would name.
			full := forgeVersionsRel + "/" + outsideVersion + "/unix_args.txt"
			if linked == forgeVersionsRel+"/1.20.1-47.2.0" {
				full = forgeArgsRel
			}
			plant(t, filepath.Join(outside, filepath.FromSlash(strings.TrimPrefix(full, linked+"/"))), outsideArgs)
			symlink(t, outside, filepath.Join(dir, filepath.FromSlash(linked)))

			plan, logged, err := planForge(t, dir)
			assertNotInstalled(t, plan, err)
			assertSkipLogged(t, logged, linked)
			if !strings.Contains(logged, "server_id=s1") {
				t.Fatalf("worker log = %q, want the warning to name the server", logged)
			}
		})
	}
}

// A regular file where a directory component is expected has nothing beneath it.
func TestLaunchPlanSkipsANonDirectoryComponent(t *testing.T) {
	for _, rel := range []string{"libraries/net", forgeVersionsRel + "/1.20.1-47.2.0"} {
		t.Run(rel, func(t *testing.T) {
			dir := t.TempDir()
			plant(t, filepath.Join(dir, filepath.FromSlash(rel)), "x")

			plan, _, err := planForge(t, dir)
			assertNotInstalled(t, plan, err)
		})
	}
}

// An args entry that is not a regular file is not an args file, whatever it is
// and wherever a link at it points. A FIFO must not block the planning either.
func TestLaunchPlanCountsOnlyARegularArgsFile(t *testing.T) {
	cases := map[string]func(t *testing.T, dir, outside, args string){
		"symlink to an outside file": func(t *testing.T, _, outside, args string) {
			plant(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
			symlink(t, filepath.Join(outside, "unix_args.txt"), args)
		},
		"symlink to a file inside the working set": func(t *testing.T, dir, _, args string) {
			plant(t, filepath.Join(dir, "real_args.txt"), "x")
			symlink(t, filepath.Join(dir, "real_args.txt"), args)
		},
		"dangling symlink": func(t *testing.T, _, outside, args string) {
			symlink(t, filepath.Join(outside, "missing"), args)
		},
		"directory": func(t *testing.T, _, _, args string) { mkdir(t, args) },
		"fifo":      func(t *testing.T, _, _, args string) { mkfifo(t, args) },
	}
	for name, tamper := range cases {
		t.Run(name, func(t *testing.T) {
			dir, outside := t.TempDir(), t.TempDir()
			tamper(t, dir, outside, filepath.Join(dir, filepath.FromSlash(forgeArgsRel)))

			plan, logged, err := planForge(t, dir)
			assertNotInstalled(t, plan, err)
			assertSkipLogged(t, logged, forgeArgsRel)
		})
	}
}

// A tampered version beside a real one is not a second candidate: the real args
// file launches, where following the link made the launch ambiguous.
func TestLaunchPlanLaunchesTheRealVersionBesideATamperedOne(t *testing.T) {
	cases := map[string]func(t *testing.T, outside, version string){
		"version linked outside": func(t *testing.T, outside, version string) {
			plant(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
			symlink(t, outside, version)
		},
		"args file is a directory": func(t *testing.T, _, version string) {
			mkdir(t, filepath.Join(version, "unix_args.txt"))
		},
	}
	for name, tamper := range cases {
		t.Run(name, func(t *testing.T) {
			dir, outside := t.TempDir(), t.TempDir()
			writeArgsfile(t, dir)
			tamper(t, outside, filepath.Join(dir, forgeVersionsRel, "1.20.1-47.3.0"))

			plan, _, err := planForge(t, dir)
			assertForgeLaunch(t, plan, err)
		})
	}
}

// user_jvm_args.txt goes on the command line only as a regular file. A link reads
// as absent whether its target exists or not, so the command line tells the
// server nothing about a host path it cannot see itself.
func TestLaunchPlanCountsOnlyARegularUserJVMArgsFile(t *testing.T) {
	cases := map[string]func(t *testing.T, outside, userArgs string){
		"symlink to an existing outside path": func(t *testing.T, outside, userArgs string) {
			plant(t, filepath.Join(outside, "exists"), "x")
			symlink(t, filepath.Join(outside, "exists"), userArgs)
		},
		"symlink to a missing outside path": func(t *testing.T, outside, userArgs string) {
			symlink(t, filepath.Join(outside, "missing"), userArgs)
		},
		"symlink to an outside directory": func(t *testing.T, outside, userArgs string) {
			symlink(t, outside, userArgs)
		},
		"directory": func(t *testing.T, _, userArgs string) { mkdir(t, userArgs) },
		"fifo":      func(t *testing.T, _, userArgs string) { mkfifo(t, userArgs) },
		"absent":    func(*testing.T, string, string) {},
	}
	for name, tamper := range cases {
		t.Run(name, func(t *testing.T) {
			dir, outside := t.TempDir(), t.TempDir()
			writeArgsfile(t, dir)
			tamper(t, outside, filepath.Join(dir, userJVMArgs))

			plan, _, err := planForge(t, dir)
			assertForgeLaunch(t, plan, err)
		})
	}

	t.Run("regular file", func(t *testing.T) {
		dir := t.TempDir()
		writeArgsfile(t, dir)
		plant(t, filepath.Join(dir, userJVMArgs), "-Xmx1G\n")

		plan, _, err := planForge(t, dir)
		want := execution.LaunchPlan{LaunchArgs: append([]string{"@/data/" + userJVMArgs}, wantForgeLaunch...)}
		if err != nil || !reflect.DeepEqual(plan, want) {
			t.Fatalf("plan = %+v, err = %v; want %+v", plan, err, want)
		}
	})
}

// The legacy launch jar is found the same way: only a regular file in the
// working-set root counts.
func TestLegacyForgeJarCountsOnlyARegularFile(t *testing.T) {
	const jar = "forge-1.12.2-14.23.5.2860.jar"
	resolve := func(dir string) (string, bool, error) {
		return execution.ResolveLegacyForgeJar(containerPathResolver(dir, slog.New(slog.DiscardHandler)))
	}

	cases := map[string]func(t *testing.T, outside, path string){
		"symlink to an outside jar": func(t *testing.T, outside, path string) {
			plant(t, filepath.Join(outside, jar), "x")
			symlink(t, filepath.Join(outside, jar), path)
		},
		"dangling symlink": func(t *testing.T, outside, path string) {
			symlink(t, filepath.Join(outside, "missing"), path)
		},
		"directory": func(t *testing.T, _, path string) { mkdir(t, path) },
	}
	for name, tamper := range cases {
		t.Run(name, func(t *testing.T) {
			dir, outside := t.TempDir(), t.TempDir()
			tamper(t, outside, filepath.Join(dir, jar))

			if rel, found, err := resolve(dir); err != nil || found {
				t.Fatalf("legacy jar = %q, found = %v, err = %v; want not found", rel, found, err)
			}
		})
	}

	t.Run("regular file beside a planted one", func(t *testing.T) {
		dir, outside := t.TempDir(), t.TempDir()
		plant(t, filepath.Join(dir, jar), "x")
		symlink(t, outside, filepath.Join(dir, "forge-planted.jar"))

		if rel, found, err := resolve(dir); err != nil || !found || rel != jar {
			t.Fatalf("legacy jar = %q, found = %v, err = %v; want %s", rel, found, err, jar)
		}
	})
}

// A failure that is not "nothing there" is reported, not read as "not installed".
func TestLaunchPlanReportsAnUnreadableWorkingSet(t *testing.T) {
	dir := t.TempDir()
	writeArgsfile(t, dir)
	orig := launchOpenAt
	t.Cleanup(func() { launchOpenAt = orig })
	launchOpenAt = func(dirFd int, entry string, flags int, mode uint32) (int, error) {
		if entry == "net" {
			return -1, unix.EIO
		}
		return orig(dirFd, entry, flags, mode)
	}

	_, _, err := planForge(t, dir)
	if !errors.Is(err, unix.EIO) || !strings.Contains(err.Error(), "libraries/net") {
		t.Fatalf("err = %v, want EIO naming libraries/net", err)
	}
}

// beforeLaunchOpen runs swap once, immediately before the planning opens the
// directory called name — after it has listed the parent and matched the name.
func beforeLaunchOpen(t *testing.T, name string, swap func()) {
	t.Helper()
	orig := launchOpenAt
	t.Cleanup(func() { launchOpenAt = orig })
	done := false
	launchOpenAt = func(dirFd int, entry string, flags int, mode uint32) (int, error) {
		if !done && filepath.Base(entry) == name {
			done = true
			swap()
		}
		return orig(dirFd, entry, flags, mode)
	}
}

// beforeLaunchStat runs swap once, immediately before the planning examines the
// entry called name.
func beforeLaunchStat(t *testing.T, name string, swap func()) {
	t.Helper()
	orig := launchStatAt
	t.Cleanup(func() { launchStatAt = orig })
	done := false
	launchStatAt = func(dirFd int, entry string, st *unix.Stat_t, flags int) error {
		if !done && entry == name {
			done = true
			swap()
		}
		return orig(dirFd, entry, st, flags)
	}
}

// A version directory swapped for a link to an outside directory after the
// planning listed it, but before it opened it, is refused.
func TestLaunchPlanRefusesAVersionSwappedInAfterTheListing(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	plant(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
	writeArgsfile(t, dir)
	version := filepath.Join(dir, forgeVersionsRel, "1.20.1-47.2.0")

	beforeLaunchOpen(t, "1.20.1-47.2.0", func() {
		if err := os.RemoveAll(version); err != nil {
			t.Error(err)
		}
		if err := os.Symlink(outside, version); err != nil {
			t.Error(err)
		}
	})

	plan, logged, err := planForge(t, dir)
	assertNotInstalled(t, plan, err)
	assertSkipLogged(t, logged, forgeVersionsRel+"/1.20.1-47.2.0")
}

// An earlier component swapped for a link while the planning is further down: the
// descent continues from the directory it holds open, so the version it reports
// is the one it listed there — never a name from behind the link.
func TestLaunchPlanDescendsFromTheDirectoryItOpened(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	plant(t, filepath.Join(outside, "net/minecraftforge/forge", outsideVersion, "unix_args.txt"), outsideArgs)
	writeArgsfile(t, dir)
	libraries, moved := filepath.Join(dir, "libraries"), filepath.Join(dir, "moved")

	beforeLaunchOpen(t, "forge", func() {
		if err := os.Rename(libraries, moved); err != nil {
			t.Error(err)
		}
		if err := os.Symlink(outside, libraries); err != nil {
			t.Error(err)
		}
	})

	plan, _, err := planForge(t, dir)
	assertForgeLaunch(t, plan, err)
}

// An args file swapped for a link to an outside file after the planning listed
// its directory: what is examined is the entry itself, never the link's target.
func TestLaunchPlanRefusesAnArgsFileSwappedForALink(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	plant(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
	writeArgsfile(t, dir)
	args := filepath.Join(dir, filepath.FromSlash(forgeArgsRel))

	beforeLaunchStat(t, "unix_args.txt", func() {
		if err := os.Remove(args); err != nil {
			t.Error(err)
		}
		if err := os.Symlink(filepath.Join(outside, "unix_args.txt"), args); err != nil {
			t.Error(err)
		}
	})

	plan, _, err := planForge(t, dir)
	assertNotInstalled(t, plan, err)
}

// forgeLoggingDriver is forgeDriver with its log captured.
func forgeLoggingDriver(docker dockerAPI) (*Driver, *syncBuffer) {
	logged := &syncBuffer{}
	return New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{
		WorkerID:             "w1",
		StopTimeout:          50 * time.Millisecond,
		GameBindIP:           "0.0.0.0",
		ReadinessTimeout:     20 * time.Millisecond,
		ConflictPollInterval: time.Millisecond,
		ConflictDeadline:     100 * time.Millisecond,
		Logger:               slog.New(slog.NewTextHandler(logged, nil)),
	}), logged
}

// created returns every container the fake was asked to create, in order.
func (f *forgeFakeDocker) created() []CreateSpec {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]CreateSpec(nil), f.createSpecs...)
}

// The retry of a failed install runs the installer, whatever the first attempt
// left in the working set. Planted during attempt 0, a link to an outside tree
// holding an args file — or a directory called unix_args.txt, which the cleanup
// leaves in place — used to make the re-plan say "installed", so the retry
// container was created with no installer command at all (`java
// -Duser.home=/tmp`) and the start died on Java's usage text (issue #3352).
func TestForgeInstallRetryRunsTheInstallerOverAPlantedArgsFile(t *testing.T) {
	cases := map[string]func(t *testing.T, dir, outside string){
		"versions directory linked outside": func(t *testing.T, dir, outside string) {
			plant(t, filepath.Join(outside, outsideVersion, "unix_args.txt"), outsideArgs)
			symlink(t, outside, filepath.Join(dir, forgeVersionsRel))
		},
		"args file is a directory": func(t *testing.T, dir, _ string) {
			mkdir(t, filepath.Join(dir, filepath.FromSlash(forgeArgsRel)))
		},
		"args file linked outside": func(t *testing.T, dir, outside string) {
			plant(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
			args := filepath.Join(dir, filepath.FromSlash(forgeArgsRel))
			mkdir(t, filepath.Dir(args))
			// Planted after the retry's cleanup, which would unlink it, and before
			// its re-plan — as a process still writing the working set could.
			beforeLaunchOpen(t, "libraries", func() {
				if err := os.Symlink(filepath.Join(outside, "unix_args.txt"), args); err != nil {
					t.Error(err)
				}
			})
		},
	}
	for name, tamper := range cases {
		t.Run(name, func(t *testing.T) {
			prev := installRetryBackoff
			installRetryBackoff = []time.Duration{time.Millisecond, time.Millisecond}
			t.Cleanup(func() { installRetryBackoff = prev })

			dir, outside := t.TempDir(), t.TempDir()
			docker := newForgeFakeDocker()
			installID := "mcsd-s1-install"
			docker.waitGates[installID] = make(chan waitResult, 3)
			for range 3 {
				docker.waitGates[installID] <- waitResult{code: 1}
			}
			first := true
			docker.onCreateHook = func(CreateSpec) {
				if first {
					first = false
					tamper(t, dir, outside)
				}
			}

			d, logged := forgeLoggingDriver(docker)
			inst, err := d.Start(context.Background(), forgeSpec(dir))
			if err != nil {
				t.Fatalf("Start: %v", err)
			}
			ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
			if !strings.Contains(ev.Detail, "after 3 attempts") {
				t.Fatalf("crash detail = %q, want the installer's own failure after 3 attempts", ev.Detail)
			}
			// Each retry warned about what it passed over, against this server.
			if !strings.Contains(logged.String(), "level=WARN") {
				t.Fatalf("worker log = %q, want the planted entry warned about", logged.String())
			}
			assertWarningsNameTheServer(t, logged.String())

			created := docker.created()
			if len(created) != 3 {
				t.Fatalf("created %d containers, want 3 install attempts", len(created))
			}
			for n, spec := range created {
				if spec.Name != installID || !reflect.DeepEqual(spec.Cmd, wantInstallCmd) {
					t.Fatalf("attempt %d: container %s Cmd = %q, want %s running %q", n, spec.Name, spec.Cmd, installID, wantInstallCmd)
				}
			}
		})
	}
}

// A retry whose working set still holds a real args file — the cleanup could not
// remove it — has no installer command to run. It fails with that reason, logged
// against the server, instead of creating a container with an empty command.
func TestForgeInstallRetryRefusesToRunWithoutAnInstallerCommand(t *testing.T) {
	prev := installRetryBackoff
	installRetryBackoff = []time.Duration{time.Millisecond, time.Millisecond}
	t.Cleanup(func() { installRetryBackoff = prev })
	orig := forgeCleanUnlinkAt
	t.Cleanup(func() { forgeCleanUnlinkAt = orig })
	forgeCleanUnlinkAt = func(int, string, int) error { return unix.EACCES }

	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	docker.waitGates[installID] = make(chan waitResult, 1)
	docker.waitGates[installID] <- waitResult{code: 1}
	// The failed attempt "wrote" its args file before it died.
	docker.onCreateHook = func(CreateSpec) { writeArgsfile(t, dir) }

	d, logged := forgeLoggingDriver(docker)
	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
	if !strings.Contains(ev.Detail, "forge install retry failed") || !strings.Contains(ev.Detail, "no installer command") {
		t.Fatalf("crash detail = %q, want the retry refused for want of an installer command", ev.Detail)
	}
	if got := docker.created(); len(got) != 1 {
		t.Fatalf("created %d containers, want only the first install attempt", len(got))
	}
	if log := logged.String(); !strings.Contains(log, "level=WARN") || !strings.Contains(log, "no installer command") || !strings.Contains(log, "server_id=s1") {
		t.Fatalf("worker log = %q, want a warning naming the server and the reason", log)
	}
}

// The cleanup's warnings about what it left alone name the server, so a tampered
// working set can be told apart on a Worker running several (issue #3352).
func TestForgeStartCleanupWarningsNameTheServer(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	symlink(t, outside, filepath.Join(dir, "libraries"))
	mkdir(t, filepath.Join(dir, "forge-1.12.2-14.23.5.2860.jar"))
	docker := newForgeFakeDocker()

	d, logged := forgeLoggingDriver(docker)
	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	docker.exit("mcsd-s1-install", 0, nil)
	drainTo(t, inst.Events(), execution.StateCrashed)

	for _, msg := range []string{
		"stale Forge artifacts not cleaned",
		"stale Forge artifact not removed",
		"ignored by the launch planning",
	} {
		if !strings.Contains(logged.String(), msg) {
			t.Errorf("worker log = %q, want a %q warning", logged.String(), msg)
		}
	}
	assertWarningsNameTheServer(t, logged.String())
}

// assertWarningsNameTheServer fails if any warning in the log lacks the server's
// id.
func assertWarningsNameTheServer(t *testing.T, logged string) {
	t.Helper()
	for _, line := range strings.Split(logged, "\n") {
		if strings.Contains(line, "level=WARN") && !strings.Contains(line, "server_id=s1") {
			t.Errorf("log line %q carries no server_id", line)
		}
	}
}
