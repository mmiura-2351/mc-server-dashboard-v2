package containerdriver

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// The tests below drive an install whose Wait result was lost to a daemon blip:
// Wait returns a transport error (and exit code 0, as the real client does), and
// the re-inspect finds the container exited but still present. The exit status
// the daemon kept on that container — not the lost Wait result — must decide
// whether the install failed and why (issue #1093, PR #3280 review).

var errWaitTransport = errors.New("containerdriver: POST /wait: EOF")

// lostWaitInstall scripts the first install attempt's Wait as a transport error
// followed by `later` ordinary results for the retries, and Inspect as `steps`.
func lostWaitInstall(t *testing.T, docker *forgeFakeDocker, steps []inspectStep, later ...waitResult) {
	t.Helper()
	docker.waitGates[forgeInstallID] = make(chan waitResult, 1+len(later))
	docker.waitGates[forgeInstallID] <- waitResult{err: errWaitTransport}
	for _, r := range later {
		docker.waitGates[forgeInstallID] <- r
	}
	docker.inspectGate = make(chan inspectStep, len(steps))
	for _, s := range steps {
		docker.inspectGate <- s
	}
	prevDeadline, prevInterval := waitTransportProbeDeadline, waitTransportProbeInterval
	waitTransportProbeDeadline, waitTransportProbeInterval = 100*time.Millisecond, time.Millisecond
	t.Cleanup(func() {
		waitTransportProbeDeadline, waitTransportProbeInterval = prevDeadline, prevInterval
	})
}

// An OOM-killed install whose Wait result was lost is still an out-of-memory
// crash: the inspected container carries the flag and the exit status.
func TestInstallLostWaitRecoversOOMKilledFromInspect(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	lostWaitInstall(t, docker, []inspectStep{
		{info: ContainerInfo{ID: forgeInstallID, OOMKilled: true, ExitCode: 137}},
	})
	spec := forgeSpec(dir)
	spec.MemoryLimitMB = 512

	inst := startForgeInstall(t, docker, spec)

	ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
	if ev.CrashReason != execution.CrashReasonForgeInstallOutOfMemory {
		t.Fatalf("crash reason = %q, want out of memory (detail %q)", ev.CrashReason, ev.Detail)
	}
	if got := docker.names(); len(got) != 1 {
		t.Fatalf("created containers = %v, want the one install attempt (no retry, no launch)", got)
	}
}

// A Java-incompatible install whose Wait result was lost keeps its diagnostic:
// the inspected exit status says it failed, and the output says why.
func TestInstallLostWaitRecoversJavaIncompatibleFromInspect(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	docker.logBodies[forgeInstallID] = string(frame(dockerStreamStderr,
		"java.lang.UnsupportedClassVersionError: net/minecraftforge/installer/SimpleInstaller\n"))
	lostWaitInstall(t, docker, []inspectStep{
		{info: ContainerInfo{ID: forgeInstallID, ExitCode: 1}},
	})

	inst := startForgeInstall(t, docker, forgeSpec(dir))

	ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
	if ev.CrashReason != execution.CrashReasonForgeInstallJavaIncompatible {
		t.Fatalf("crash reason = %q, want java incompatible (detail %q)", ev.CrashReason, ev.Detail)
	}
	if got := docker.names(); len(got) != 1 {
		t.Fatalf("created containers = %v, want the one install attempt (no retry, no launch)", got)
	}
}

// A generic failed install whose Wait result was lost is retried like any other
// non-zero exit, and reports the recovered exit code if the retries fail too. The
// two later Inspect steps answer the ordinary attempts' OOM probes.
func TestInstallLostWaitRecoversNonZeroExitAndRetries(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	lostWaitInstall(t, docker, []inspectStep{
		{info: ContainerInfo{ID: forgeInstallID, ExitCode: 1}},
		{err: errNotFound},
		{err: errNotFound},
	}, waitResult{code: 1}, waitResult{code: 1})

	inst := startForgeInstall(t, docker, forgeSpec(dir))

	ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
	if ev.CrashReason != execution.CrashReasonForgeInstallFailed {
		t.Fatalf("crash reason = %q, want the generic install failure (detail %q)", ev.CrashReason, ev.Detail)
	}
	if !strings.Contains(ev.Detail, "after 3 attempts") || !strings.Contains(ev.Detail, "exited with code 1") {
		t.Fatalf("crash detail = %q, want the attempt count and the exit code", ev.Detail)
	}
	if got := docker.names(); len(got) != 3 {
		t.Fatalf("created containers = %v, want 3 install attempts", got)
	}
}

// The first retry must be driven by the RECOVERED exit status, not by a later
// attempt: with only the first attempt failing, a second install is created and
// the server launches.
func TestInstallLostWaitRecoveredFailureRetriesAndThenLaunches(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	lostWaitInstall(t, docker, []inspectStep{
		{info: ContainerInfo{ID: forgeInstallID, ExitCode: 1}},
	}, waitResult{code: 0})
	creates := 0
	docker.onCreateHook = func(spec CreateSpec) {
		if creates++; creates == 2 && spec.Name == forgeInstallID {
			writeArgsfile(t, dir)
		}
	}

	inst := startForgeInstall(t, docker, forgeSpec(dir))

	drainTo(t, inst.Events(), execution.StateRunning)
	if got := docker.names(); len(got) != 3 || got[2] != "mcsd-s1" {
		t.Fatalf("created containers = %v, want 2 installs + 1 launch", got)
	}
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// A successful install whose Wait result was lost launches — also when its output
// happens to contain the diagnostic markers, which only explain a failure.
func TestInstallLostWaitRecoversSuccessfulExitAndLaunches(t *testing.T) {
	for name, output := range map[string]string{
		"plain output":   "The server installed successfully\n",
		"marker strings": "java.lang.OutOfMemoryError java.lang.UnsupportedClassVersionError\n",
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			docker := newForgeFakeDocker()
			docker.logBodies[forgeInstallID] = string(frame(dockerStreamStdout, output))
			lostWaitInstall(t, docker, []inspectStep{
				{info: ContainerInfo{ID: forgeInstallID, ExitCode: 0}},
			})
			// The install container "produces" the args file, as a real install does.
			docker.onCreateHook = func(CreateSpec) { writeArgsfile(t, dir) }

			inst := startForgeInstall(t, docker, forgeSpec(dir))

			drainTo(t, inst.Events(), execution.StateRunning)
			if got := docker.names(); len(got) != 2 || got[1] != "mcsd-s1" {
				t.Fatalf("created containers = %v, want 1 install + 1 launch", got)
			}
			docker.exit("mcsd-s1", 0, nil)
			drainClosed(inst.Events())
		})
	}
}

// When the container is already GONE its exit status is unrecoverable, so the
// produced artifacts stay the authority (issue #895): an install that left its
// args file launches even though its output carries a marker.
func TestInstallLostWaitContainerGoneWithMarkersStillLaunchesOnArtifacts(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	docker.logBodies[forgeInstallID] = string(frame(dockerStreamStdout, "java.lang.OutOfMemoryError\n"))
	lostWaitInstall(t, docker, []inspectStep{{err: errNotFound}})
	docker.onCreateHook = func(CreateSpec) { writeArgsfile(t, dir) }

	inst := startForgeInstall(t, docker, forgeSpec(dir))

	drainTo(t, inst.Events(), execution.StateRunning)
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}
