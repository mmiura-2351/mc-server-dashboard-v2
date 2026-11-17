package containerdriver

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// noDocker is a dockerAPI with no endpoint behind it: it embeds a nil one, so a
// call of any endpoint panics.
type noDocker struct{ dockerAPI }

// HasLeftovers answers from the record alone, with no Docker call (issue #3679):
// the instance manager asks it for every delete report, on every Worker, and for
// every scratch dir holding no working set at each registration (issue #3794).
// The driver here has no Docker behind it, so a call of any endpoint, however
// bounded, fails the test.
func TestHasLeftoversAnswersFromTheRecord(t *testing.T) {
	d := New(noDocker{}, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{WorkerID: "w1"})
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("HasLeftovers called Docker: %v", r)
		}
	}()

	if d.HasLeftovers("s1") {
		t.Fatal("HasLeftovers = true with nothing recorded, want false")
	}
	d.unremoved.add("s1", "container-1")
	if !d.HasLeftovers("s1") || d.HasLeftovers("s2") {
		t.Fatalf("HasLeftovers(s1, s2) = %v, %v; want true, false", d.HasLeftovers("s1"), d.HasLeftovers("s2"))
	}
	if got := unremovedOf(d, "s1"); !slices.Equal(got, []string{"container-1"}) {
		t.Fatalf("recorded = %v, want the record left as it was", got)
	}
}

// A recorded leftover the daemon still knows makes the server busy whatever the
// daemon lists: the start the daemon did not answer may still run the container
// after a List showed it created. So its inspect answers, without a List. One
// whose inspect fails or stalls is busy too, since it may be running.
func TestBusyReportsARecordedLeftover(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*fakeDocker)
	}{
		{name: "present", setup: func(*fakeDocker) {}},
		{name: "inspect fails", setup: func(f *fakeDocker) { f.inspectErr = errors.New("daemon busy") }},
		{name: "inspect stalls", setup: func(f *fakeDocker) { f.inspectBlocksUntilCtxDone = true }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			docker := newFakeDocker()
			docker.listBlocksUntilCtxDone = true
			tc.setup(docker)
			d := newTestDriver(docker, nil, errors.New("no rcon"))
			d.conflictDeadline = time.Hour
			d.exitRemoveTimeout = 20 * time.Millisecond
			d.unremoved.add("s1", "container-1")

			err := busyWithin(t, d, "s1")
			if err == nil || !strings.Contains(err.Error(), "container container-1") || !errors.Is(err, execution.ErrBackendUnavailable) {
				t.Fatalf("Busy = %v, want the recorded leftover named, classified as the backend's unavailability", err)
			}
			if got := unremovedOf(d, "s1"); !slices.Equal(got, []string{"container-1"}) {
				t.Fatalf("recorded = %v, want the record left as it was", got)
			}
		})
	}
}

// A recorded leftover the daemon no longer knows is gone: Busy drops it and
// answers from the List. Without that, a removal answered 409 that then went
// through would keep the server's final snapshot refused after a healthy run,
// until its next start.
func TestBusyDropsARecordedLeftoverTheDaemonNoLongerKnows(t *testing.T) {
	docker := newFakeDocker()
	docker.inspectErr = fmt.Errorf("%w: status 404", errNotFound)
	d := newTestDriver(docker, nil, errors.New("no rcon"))
	d.unremoved.add("s1", "container-1")

	if err := busyWithin(t, d, "s1"); err != nil {
		t.Fatalf("Busy = %v, want nil once the leftover is gone", err)
	}
	if got := unremovedOf(d, "s1"); len(got) != 0 {
		t.Fatalf("recorded = %v, want the record cleared", got)
	}
}

// A running container labelled with the server's id makes it busy even with
// nothing recorded: the record is lost on a Worker restart, and a container the
// daemon ran after refusing its start is never recorded.
func TestBusyReportsARunningContainerOfTheServer(t *testing.T) {
	docker := newFakeDocker()
	docker.runningID = "container-9"
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	if err := busyWithin(t, d, "s1"); err == nil || !strings.Contains(err.Error(), "in use") || !errors.Is(err, execution.ErrBackendUnavailable) {
		t.Fatalf("Busy = %v, want the running container to make the server busy, classified as the backend's unavailability", err)
	}
}

// A daemon that cannot list the server's containers leaves it busy: the answer
// fails closed.
func TestBusyFailsClosedWhenTheDaemonCannotList(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*fakeDocker)
	}{
		{name: "list fails", setup: func(f *fakeDocker) { f.listErr = errors.New("daemon down") }},
		{name: "list stalls", setup: func(f *fakeDocker) { f.listBlocksUntilCtxDone = true }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			docker := newFakeDocker()
			tc.setup(docker)
			d := newTestDriver(docker, nil, errors.New("no rcon"))

			if err := busyWithin(t, d, "s1"); err == nil || !strings.Contains(err.Error(), "cannot establish") || !errors.Is(err, execution.ErrBackendUnavailable) {
				t.Fatalf("Busy = %v, want a server whose containers cannot be listed treated as busy, classified as the backend's unavailability", err)
			}
		})
	}
}

// A server with nothing recorded and only exited containers is not busy.
func TestBusyIsNilWhenNothingOfTheServerRuns(t *testing.T) {
	docker := newFakeDocker()
	docker.listResult = []Container{{ID: "a", Name: "/mcsd-s1", State: "exited"}}
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	if err := busyWithin(t, d, "s1"); err != nil {
		t.Fatalf("Busy = %v, want nil", err)
	}
}

// EndLeftovers gives a recorded leftover the treatment the server's next start
// gives it: a graceful stop that watches the working set it is given, then a
// removal. The record is cleared, and the server is then no longer busy.
func TestEndLeftoversStopsAndRemovesARecordedLeftover(t *testing.T) {
	docker := newFakeDocker()
	docker.runningID = "container-2"
	docker.leftoverWaits = map[string]chan struct{}{"container-2": make(chan struct{})}
	d := newTestDriver(docker, nil, errors.New("no rcon"))
	var watched []string
	d.worldProgress = func(dir string) func() (execution.WorldProgress, error) {
		watched = append(watched, dir)
		return func() (execution.WorldProgress, error) { return execution.WorldProgress{}, nil }
	}
	d.unremoved.add("s1", "container-2")
	// Every bound is long, so only the leftover's exit can end its stop.
	d.stopTimeout, d.stopCap, d.unremovedStopTimeout = time.Hour, time.Hour, time.Hour

	if err := d.EndLeftovers(context.Background(), "s1", "/scratch/s1"); err != nil {
		t.Fatalf("EndLeftovers = %v, want the leftover stopped and removed", err)
	}
	docker.mu.Lock()
	ops := append([]string(nil), docker.ops...)
	docker.mu.Unlock()
	if want := []string{"stop container-2", "remove container-2"}; !slices.Equal(ops, want) {
		t.Fatalf("ops = %v, want %v", ops, want)
	}
	if !slices.Equal(watched, []string{"/scratch/s1"}) {
		t.Fatalf("watched = %v, want the server's working set", watched)
	}
	if got := unremovedOf(d, "s1"); len(got) != 0 {
		t.Fatalf("recorded = %v, want the record cleared", got)
	}
	if err := busyWithin(t, d, "s1"); err != nil {
		t.Fatalf("Busy after EndLeftovers = %v, want nil", err)
	}
}

// A leftover EndLeftovers cannot remove stays recorded, and the error names it.
// The server stays busy.
func TestEndLeftoversKeepsALeftoverItCannotRemove(t *testing.T) {
	docker := newFakeDocker()
	docker.removeErrs = []error{errors.New("daemon busy")}
	d := newTestDriver(docker, nil, errors.New("no rcon"))
	d.unremoved.add("s1", "container-1")

	err := d.EndLeftovers(context.Background(), "s1", "/scratch/s1")
	if err == nil || !strings.Contains(err.Error(), "container container-1") || !errors.Is(err, execution.ErrBackendUnavailable) {
		t.Fatalf("EndLeftovers = %v, want a refusal naming container-1, classified as the backend's unavailability", err)
	}
	if got := unremovedOf(d, "s1"); !slices.Equal(got, []string{"container-1"}) {
		t.Fatalf("recorded = %v, want container-1 kept", got)
	}
	if err := busyWithin(t, d, "s1"); err == nil {
		t.Fatal("Busy = nil, want the kept leftover to keep the server busy")
	}
}

// busyWithin runs d.Busy, failing if it is still blocked after the guard: the
// answer is bounded.
func busyWithin(t *testing.T, d *Driver, serverID string) error {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- d.Busy(context.Background(), serverID) }()
	select {
	case err := <-done:
		return err
	case <-time.After(10 * time.Second):
		t.Fatal("Busy still blocks past its bound")
		return nil
	}
}
