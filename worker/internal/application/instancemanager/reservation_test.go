package instancemanager

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// gatedDriver blocks inside Start until release is closed, so a test can hold the
// first StartServer mid-driver.Start (modeling the host-process RealSpawn / Forge
// create window from issue #780) and issue the re-issued duplicate while the
// original is still in flight. startErr, when set, fails every Start.
type gatedDriver struct {
	mu       sync.Mutex
	started  int
	entered  chan struct{} // signaled each time Start is entered
	release  chan struct{} // Start returns once this is closed
	startErr error
}

func newGatedDriver() *gatedDriver {
	return &gatedDriver{entered: make(chan struct{}, 8), release: make(chan struct{})}
}

func (d *gatedDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.entered <- struct{}{}
	<-d.release
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.startErr != nil {
		return nil, d.startErr
	}
	d.started++
	return newFakeInstance(spec.ServerID), nil
}

func (d *gatedDriver) startCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.started
}

// gatedTransfer blocks inside Hydrate until release is closed, mirroring the
// long-running hydrate window an old stream's lane can still be writing when the
// re-issued HydrateTrigger arrives (issue #780).
type gatedTransfer struct {
	mu       sync.Mutex
	hydrated int
	entered  chan struct{}
	release  chan struct{}
}

func newGatedTransfer() *gatedTransfer {
	return &gatedTransfer{entered: make(chan struct{}, 8), release: make(chan struct{})}
}

func (t *gatedTransfer) Hydrate(_ context.Context, _, _, _ string) (uint64, error) {
	t.entered <- struct{}{}
	<-t.release
	t.mu.Lock()
	defer t.mu.Unlock()
	t.hydrated++
	return 0, nil
}

func (t *gatedTransfer) Snapshot(_ context.Context, _, _, _ string) (uint64, error) {
	return 0, nil
}

func (t *gatedTransfer) hydrateCount() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.hydrated
}

// awaitEnter blocks until c receives one signal or the deadline elapses.
func awaitEnter(t *testing.T, c <-chan struct{}) {
	t.Helper()
	select {
	case <-c:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for the gated operation to be entered")
	}
}

// A duplicate StartServer re-issued while the original is mid-driver.Start (the
// reconnect-redelivery window, issue #780) is rejected with INVALID_STATE and the
// driver is started exactly once — never two instances for one server.
func TestConcurrentDuplicateStartStartsDriverOnce(t *testing.T) {
	d := newGatedDriver()
	m := newManager(t, d, nil)

	firstDone := make(chan session.CommandResult, 1)
	go func() { firstDone <- m.Handle(context.Background(), startCmd()) }()

	// The first start is now blocked inside driver.Start; the reservation is held.
	awaitEnter(t, d.entered)

	dup := m.Handle(context.Background(), startCmd())
	if dup.Success || dup.ErrorCode != session.CommandErrorInvalidState {
		t.Fatalf("duplicate start = %+v, want INVALID_STATE failure", dup)
	}

	close(d.release)
	first := <-firstDone
	if !first.Success {
		t.Fatalf("first start = %+v, want success", first)
	}
	if d.startCount() != 1 {
		t.Fatalf("driver started %d times, want exactly 1", d.startCount())
	}
}

// When a reserved start fails (driver.Start errors), the reservation is released
// so a retry over the same id can proceed (issue #780): the reservation must not
// wedge the id after a failure.
func TestReservationReleasedAfterStartFailure(t *testing.T) {
	d := newGatedDriver()
	d.startErr = context.DeadlineExceeded
	m := newManager(t, d, nil)

	go func() { _ = m.Handle(context.Background(), startCmd()) }()
	awaitEnter(t, d.entered)
	close(d.release) // first start fails

	// A clean driver for the retry; the id must no longer be reserved.
	d2 := &fakeDriver{}
	m.drivers["host-process"] = d2
	// Wait for the failed start to finish releasing the reservation.
	deadline := time.Now().Add(2 * time.Second)
	var res session.CommandResult
	for {
		res = m.Handle(context.Background(), startCmd())
		if res.Success || time.Now().After(deadline) {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if !res.Success {
		t.Fatalf("retry after failed start = %+v, want success (reservation must release)", res)
	}
	if d2.startCount() != 1 {
		t.Fatalf("retry driver started %d times, want 1", d2.startCount())
	}
}

// A duplicate HydrateTrigger re-issued while the original is mid-transfer is
// rejected with INVALID_STATE and the transfer runs exactly once, so the two do
// not write the same working set concurrently (issue #780).
func TestConcurrentDuplicateHydrateRunsOnce(t *testing.T) {
	tr := newGatedTransfer()
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	hydrateCmd := session.Command{
		CommandID: "h1", ServerID: "s1", Kind: "HydrateTrigger",
		TransferURL: "https://api/working-set", TransferToken: "tok",
	}
	firstDone := make(chan session.CommandResult, 1)
	go func() { firstDone <- m.Handle(context.Background(), hydrateCmd) }()

	awaitEnter(t, tr.entered)

	dup := m.Handle(context.Background(), hydrateCmd)
	if dup.Success || dup.ErrorCode != session.CommandErrorInvalidState {
		t.Fatalf("duplicate hydrate = %+v, want INVALID_STATE failure", dup)
	}

	close(tr.release)
	first := <-firstDone
	if !first.Success {
		t.Fatalf("first hydrate = %+v, want success", first)
	}
	if tr.hydrateCount() != 1 {
		t.Fatalf("transfer hydrated %d times, want exactly 1", tr.hydrateCount())
	}
}
