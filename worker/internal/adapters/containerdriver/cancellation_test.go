package containerdriver

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// A recorded leftover whose removal or inspect the caller's own cancellation cut
// short is still the backend's unavailability, unlike a bounded call the caller
// cancelled (issue #3678): the container stays recorded and may still be running,
// which is leftover trouble whatever stopped the driver from finding out.
func TestARecordedLeftoverIsUnavailableEvenOnCallerCancel(t *testing.T) {
	for _, tc := range []struct {
		name string
		call func(context.Context, *Driver) error
	}{
		{"EndLeftovers", func(ctx context.Context, d *Driver) error { return d.EndLeftovers(ctx, "s1", "/scratch/s1") }},
		{"Busy", func(ctx context.Context, d *Driver) error { return d.Busy(ctx, "s1") }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			docker := newFakeDocker()
			docker.inspectBlocksUntilCtxDone = true
			d := newTestDriver(docker, nil, errors.New("no rcon"))
			d.unremoved.add("s1", "container-1")
			ctx, cancel := context.WithCancel(context.Background())
			cancel()

			err := tc.call(ctx, d)
			if !errors.Is(err, context.Canceled) || !errors.Is(err, execution.ErrBackendUnavailable) {
				t.Fatalf("err = %v, want the cancelled inspect, classified as the backend's unavailability", err)
			}
		})
	}
}

// The quiescence wait the caller cancels while it waits out a live container
// answers the caller's own error, not the backend's unavailability (issue #3673):
// the window did not run out, the caller went away.
func TestQuiescenceWaitCancelledWhileWaitingOutALiveContainerIsNotUnavailable(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	docker := &listScript{
		fakeDocker: newFakeDocker(),
		answers:    [][]Container{{{ID: "orphan", Name: "/mcsd-s1", State: "running"}}},
		onList:     func(int) { cancel() },
	}
	d, _ := hardenedDriver(docker)
	d.conflictDeadline, d.conflictPoll = time.Hour, time.Hour

	done := make(chan error, 1)
	go func() { done <- d.awaitQuiescent(ctx, "s1") }()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) || errors.Is(err, execution.ErrBackendUnavailable) {
			t.Fatalf("err = %v, want the caller's cancellation, unclassified", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the quiescence wait ignored the caller's cancellation")
	}
}
