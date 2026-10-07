package containerdriver

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

const forgeInstallID = "mcsd-s1-install"

// shrinkInstallBackoff makes the install retry backoff negligible for a test.
func shrinkInstallBackoff(t *testing.T) {
	t.Helper()
	prev := installRetryBackoff
	installRetryBackoff = []time.Duration{time.Millisecond, time.Millisecond}
	t.Cleanup(func() { installRetryBackoff = prev })
}

// startForgeInstall starts a Forge server whose args file is absent, so the
// driver runs the supervised install container.
func startForgeInstall(t *testing.T, docker *forgeFakeDocker, spec execution.InstanceSpec) execution.Instance {
	t.Helper()
	inst, err := forgeDriver(docker).Start(context.Background(), spec)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	return inst
}

// An install container the kernel OOM-killed at its memory limit (exit 137 with
// the daemon's OOMKilled flag, and nothing about it in the installer's own
// output) crashes with the out-of-memory reason and a detail naming the limit. It
// is not retried: the same limit kills the next attempt the same way (issue
// #1093).
func TestForgeInstallOOMKilledReportsOutOfMemoryWithoutRetry(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	docker.oomKilled = map[string]bool{forgeInstallID: true}
	spec := forgeSpec(dir)
	spec.MemoryLimitMB = 512

	inst := startForgeInstall(t, docker, spec)
	docker.exit(forgeInstallID, 137, nil)

	ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
	if ev.CrashReason != execution.CrashReasonForgeInstallOutOfMemory {
		t.Fatalf("crash reason = %q, want out of memory (detail %q)", ev.CrashReason, ev.Detail)
	}
	if !strings.Contains(ev.Detail, "out of memory") || !strings.Contains(ev.Detail, "512 MiB") {
		t.Fatalf("crash detail = %q, want it to name the out-of-memory failure and the 512 MiB limit", ev.Detail)
	}
	if got := docker.names(); len(got) != 1 {
		t.Fatalf("created containers = %v, want the one install attempt (no retry, no launch)", got)
	}
	if !docker.wasRemoved(forgeInstallID) {
		t.Fatal("install container not removed after the crash")
	}
}

// An installer whose JVM exhausted its heap exits 1 without the kernel being
// involved; the only evidence is java.lang.OutOfMemoryError in its output. That
// is the same out-of-memory reason, and equally not retried (issue #1093).
func TestForgeInstallJavaHeapExhaustedReportsOutOfMemoryWithoutRetry(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	docker.logBodies[forgeInstallID] = string(frame(dockerStreamStderr,
		"java.lang.OutOfMemoryError: Java heap space\nThere was an error during installation\n"))

	inst := startForgeInstall(t, docker, forgeSpec(dir))
	docker.exit(forgeInstallID, 1, nil)

	ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
	if ev.CrashReason != execution.CrashReasonForgeInstallOutOfMemory {
		t.Fatalf("crash reason = %q, want out of memory (detail %q)", ev.CrashReason, ev.Detail)
	}
	if got := docker.names(); len(got) != 1 {
		t.Fatalf("created containers = %v, want the one install attempt (no retry, no launch)", got)
	}
}

// An installer the selected Java runtime cannot load dies on
// java.lang.UnsupportedClassVersionError. It crashes with the Java-incompatible
// reason, naming the Minecraft version the runtime was selected for, and is not
// retried: the same runtime refuses the same classes (issue #1093).
func TestForgeInstallUnsupportedClassVersionReportsJavaIncompatibleWithoutRetry(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	docker.logBodies[forgeInstallID] = string(frame(dockerStreamStderr,
		`Exception in thread "main" java.lang.UnsupportedClassVersionError: `+
			"net/minecraftforge/installer/SimpleInstaller : Unsupported major.minor version 52.0\n"))

	inst := startForgeInstall(t, docker, forgeSpec(dir))
	docker.exit(forgeInstallID, 1, nil)

	ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
	if ev.CrashReason != execution.CrashReasonForgeInstallJavaIncompatible {
		t.Fatalf("crash reason = %q, want java incompatible (detail %q)", ev.CrashReason, ev.Detail)
	}
	if !strings.Contains(ev.Detail, "Java") || !strings.Contains(ev.Detail, "1.21") {
		t.Fatalf("crash detail = %q, want it to name the Java mismatch and Minecraft 1.21", ev.Detail)
	}
	if got := docker.names(); len(got) != 1 {
		t.Fatalf("created containers = %v, want the one install attempt (no retry, no launch)", got)
	}
}

// An installer that exits non-zero with none of the specific signals is a
// generic install failure: it is retried (it may be a transient download
// failure), and after the last attempt crashes with the generic install reason
// and the exit code (issue #1093, #1128).
func TestForgeInstallNonZeroExitIsRetriedThenReportsGenericFailure(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	docker.logBodies[forgeInstallID] = string(frame(dockerStreamStdout,
		"A problem installing was detected, install cannot continue\n"))
	docker.waitGates[forgeInstallID] = make(chan waitResult, 3)
	for range 3 {
		docker.waitGates[forgeInstallID] <- waitResult{code: 1}
	}

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

// Exit 137 alone is a SIGKILL from anywhere; without the daemon's OOMKilled flag
// it is not attributed to memory, so it stays a generic, retried failure rather
// than a wrong "raise the memory limit" (issue #1093).
func TestForgeInstallSigkillWithoutOOMFlagIsNotOutOfMemory(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	docker.waitGates[forgeInstallID] = make(chan waitResult, 3)
	for range 3 {
		docker.waitGates[forgeInstallID] <- waitResult{code: 137}
	}

	inst := startForgeInstall(t, docker, forgeSpec(dir))

	ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
	if ev.CrashReason != execution.CrashReasonForgeInstallFailed {
		t.Fatalf("crash reason = %q, want the generic install failure (detail %q)", ev.CrashReason, ev.Detail)
	}
	if got := docker.names(); len(got) != 3 {
		t.Fatalf("created containers = %v, want 3 install attempts", got)
	}
}

// The diagnostic markers only explain a FAILED install. An installer that prints
// one and still exits 0 succeeded, and the server launches (issue #1093).
func TestForgeInstallMarkerInOutputOfSuccessfulInstallStillLaunches(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	docker.logBodies[forgeInstallID] = string(frame(dockerStreamStdout,
		"Patching java/lang/OutOfMemoryError: java.lang.OutOfMemoryError java.lang.UnsupportedClassVersionError\n"))

	inst := startForgeInstall(t, docker, forgeSpec(dir))
	writeArgsfile(t, dir)
	docker.exit(forgeInstallID, 0, nil)

	drainTo(t, inst.Events(), execution.StateRunning)
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// A successful install that leaves nothing launchable is an install-phase crash
// too, so the client offers the install log for it (issue #1093).
func TestForgeInstallProducingNothingLaunchableCarriesTheInstallReason(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()

	inst := startForgeInstall(t, docker, forgeSpec(dir))
	docker.exit(forgeInstallID, 0, nil)

	ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
	if ev.CrashReason != execution.CrashReasonForgeInstallFailed {
		t.Fatalf("crash reason = %q, want the generic install failure (detail %q)", ev.CrashReason, ev.Detail)
	}
}

// The scanner finds a marker however the stream chunks it, including split
// across two writes, and reports nothing for output without one.
func TestInstallOutputScanFindsMarkersAcrossWrites(t *testing.T) {
	var split installOutputScan
	_, _ = split.Write([]byte("Failed to run processor: java.lang.OutOfMem"))
	_, _ = split.Write([]byte("oryError:Java heap space\n"))
	if !split.outOfMemory || split.javaIncompatible {
		t.Fatalf("split OOM marker: scan = %+v, want outOfMemory only", split)
	}

	var java installOutputScan
	_, _ = java.Write([]byte("java.lang.UnsupportedClassVersionError: x\n"))
	if !java.javaIncompatible || java.outOfMemory {
		t.Fatalf("java marker: scan = %+v, want javaIncompatible only", java)
	}

	var clean installOutputScan
	_, _ = clean.Write([]byte("The server installed successfully\n"))
	_, _ = clean.Write([]byte("java.lang.Out"))
	if clean.outOfMemory || clean.javaIncompatible {
		t.Fatalf("clean output: scan = %+v, want nothing found", clean)
	}
}
