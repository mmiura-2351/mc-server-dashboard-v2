package instancemanager

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// gatedDriver blocks inside Start until release is closed, so a test can hold the
// first StartServer mid-driver.Start (modeling the now-removed host-process RealSpawn / Forge
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

func (t *gatedTransfer) Snapshot(_ context.Context, _, _, _ string, _ uint64, _ string) (uint64, error) {
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
// reconnect-redelivery window, issue #780) is rejected with BUSY (issue #824, the
// reservation race is distinct from a settled "already running") and the driver
// is started exactly once — never two instances for one server.
func TestConcurrentDuplicateStartStartsDriverOnce(t *testing.T) {
	d := newGatedDriver()
	m := newManager(t, d, nil)

	firstDone := make(chan session.CommandResult, 1)
	go func() { firstDone <- m.Handle(context.Background(), startCmd()) }()

	// The first start is now blocked inside driver.Start; the reservation is held.
	awaitEnter(t, d.entered)

	dup := m.Handle(context.Background(), startCmd())
	if dup.Success || dup.ErrorCode != session.CommandErrorBusy {
		t.Fatalf("duplicate start = %+v, want BUSY failure", dup)
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
	m.drivers["container"] = d2
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

// gatedStopInstance is a fakeInstance whose Stop blocks until stopRelease is
// closed, modeling a DETACHED stop from a dropped stream's lane that keeps running
// (up to ~3x stopTimeout) after takeStoppableReserve has already evicted the
// instance (issue #780). stopEntered signals when Stop has been entered.
type gatedStopInstance struct {
	*fakeInstance
	stopEntered chan struct{}
	stopRelease chan struct{}
}

func newGatedStopInstance(id string) *gatedStopInstance {
	return &gatedStopInstance{
		fakeInstance: newFakeInstance(id),
		stopEntered:  make(chan struct{}, 1),
		stopRelease:  make(chan struct{}),
	}
}

func (i *gatedStopInstance) Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context) bool) error {
	i.stopEntered <- struct{}{}
	<-i.stopRelease
	return i.fakeInstance.Stop(ctx, graceful, preFallback...)
}

// gatedStopDriver hands out a single gatedStopInstance so a test can hold the
// original stop in flight while issuing the re-sent duplicate.
type gatedStopDriver struct {
	inst *gatedStopInstance
}

func (d *gatedStopDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.inst = newGatedStopInstance(spec.ServerID)
	return d.inst, nil
}

// A StopServer re-sent while a DETACHED stop is still confirming termination — the
// reconnect-redelivery window of issue #780 — must be rejected with BUSY (issue
// #824), never SERVER_NOT_FOUND: the latter makes the API unassign while the old
// process is still alive, after which a re-placed start's hydrate clobbers the live
// working set.
func TestResentStopDuringDetachedStopRejectedBusy(t *testing.T) {
	d := &gatedStopDriver{}
	m := newManager(t, d, nil).WithTransfer(&fakeTransfer{})

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}

	stopCmd := session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"}
	firstDone := make(chan session.CommandResult, 1)
	go func() { firstDone <- m.Handle(context.Background(), stopCmd) }()

	// The original stop is now blocked inside inst.Stop with the id evicted from
	// instances but reserved across the eviction -> stop-confirmed window.
	awaitEnter(t, d.inst.stopEntered)

	dup := m.Handle(context.Background(), session.Command{CommandID: "stop2", ServerID: "s1", Kind: "StopServer"})
	if dup.Success {
		t.Fatalf("re-sent stop during detached stop = %+v, want failure", dup)
	}
	if dup.ErrorCode == session.CommandErrorServerNotFound {
		t.Fatal("re-sent stop returned SERVER_NOT_FOUND: the API would unassign over a still-live process (issue #780)")
	}
	if dup.ErrorCode != session.CommandErrorBusy {
		t.Fatalf("re-sent stop = %+v, want BUSY", dup)
	}

	// Let the detached stop confirm; it must still succeed and release the id.
	close(d.inst.stopRelease)
	first := <-firstDone
	if !first.Success {
		t.Fatalf("original detached stop = %+v, want success", first)
	}
	// Once the detached stop has confirmed, the id is genuinely gone: a later stop
	// is SERVER_NOT_FOUND (the reservation was released), so the API converges.
	after := m.Handle(context.Background(), session.Command{CommandID: "stop3", ServerID: "s1", Kind: "StopServer"})
	if after.Success || after.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("stop after detached stop confirmed = %+v, want SERVER_NOT_FOUND", after)
	}
}

// A duplicate HydrateTrigger re-issued while the original is mid-transfer is
// rejected with BUSY (issue #824) and the transfer runs exactly once, so the two do
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
	if dup.Success || dup.ErrorCode != session.CommandErrorBusy {
		t.Fatalf("duplicate hydrate = %+v, want BUSY failure", dup)
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

// gatedOrphanInstance is a fakeInstance whose FIRST Stop fails (recording a
// failed-stop orphan) and whose RETRY Stop blocks until stopRelease is closed,
// modeling an orphan-retry stop that is still confirming termination. The orphan
// record therefore stays in place AND the id stays reserved across the retry's
// inst.Stop — the exact window issue #829 item 1 guards.
type gatedOrphanInstance struct {
	*fakeInstance
	stopCalls   int
	stopEntered chan struct{}
	stopRelease chan struct{}
}

func newGatedOrphanInstance(id string) *gatedOrphanInstance {
	return &gatedOrphanInstance{
		fakeInstance: newFakeInstance(id),
		stopEntered:  make(chan struct{}, 1),
		stopRelease:  make(chan struct{}),
	}
}

func (i *gatedOrphanInstance) Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context) bool) error {
	i.mu.Lock()
	i.stopCalls++
	call := i.stopCalls
	i.mu.Unlock()
	if call == 1 {
		// First stop fails: the manager records the orphan and releases the reservation.
		return errors.New("driver: process survived kill")
	}
	// Retry stop: block so it stays in flight (reserved held, orphan still recorded).
	i.stopEntered <- struct{}{}
	<-i.stopRelease
	return i.fakeInstance.Stop(ctx, graceful, preFallback...)
}

func (i *gatedOrphanInstance) stopCount() int {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.stopCalls
}

// gatedOrphanDriver hands out a single gatedOrphanInstance.
type gatedOrphanDriver struct {
	inst *gatedOrphanInstance
}

func (d *gatedOrphanDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.inst = newGatedOrphanInstance(spec.ServerID)
	return d.inst, nil
}

// A StopServer re-sent while an orphan-RETRY stop is still confirming termination
// must be rejected with BUSY (issue #824), not walk into the orphan branch and take
// the same orphan a second time (issue #829 item 1). If it did, both stops' deferred
// release would fire and the first to return would steal the still-running stop's
// reservation — worst case leaving an unreserved id. takeStoppableReserve must
// honor the reservation before the orphan branch.
func TestResentStopDuringOrphanRetryRejectedBusy(t *testing.T) {
	d := &gatedOrphanDriver{}
	m := newManager(t, d, nil).WithTransfer(&fakeTransfer{})

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	// First stop fails -> the id is now a recorded failed-stop orphan, reservation released.
	if res := m.Handle(context.Background(), session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"}); res.Success {
		t.Fatalf("first stop unexpectedly succeeded: %+v", res)
	}

	// Retry stop walks the orphan branch (reserving the id) and blocks inside inst.Stop.
	retryDone := make(chan session.CommandResult, 1)
	go func() {
		retryDone <- m.Handle(context.Background(), session.Command{CommandID: "stop2", ServerID: "s1", Kind: "StopServer"})
	}()
	awaitEnter(t, d.inst.stopEntered)

	// A re-sent stop arriving while the retry is in flight must be rejected with
	// BUSY (the reservation is honored before the orphan branch), never
	// SERVER_NOT_FOUND, and must not Stop the orphan a third time. Run it off the
	// test goroutine with a deadline: with the bug it would walk the orphan branch
	// and BLOCK inside inst.Stop, so a clean timeout failure beats a 10-minute hang.
	dupDone := make(chan session.CommandResult, 1)
	go func() {
		dupDone <- m.Handle(context.Background(), session.Command{CommandID: "stop3", ServerID: "s1", Kind: "StopServer"})
	}()
	var dup session.CommandResult
	select {
	case dup = <-dupDone:
	case <-time.After(2 * time.Second):
		t.Fatal("re-sent stop blocked: it walked the orphan branch past the live reservation instead of being rejected (issue #829 item 1)")
	}
	if dup.Success {
		t.Fatalf("re-sent stop during orphan retry = %+v, want failure", dup)
	}
	if dup.ErrorCode != session.CommandErrorBusy {
		t.Fatalf("re-sent stop = %+v, want BUSY (issue #829)", dup)
	}

	// Let the retry confirm termination; it must still succeed.
	close(d.inst.stopRelease)
	retry := <-retryDone
	if !retry.Success {
		t.Fatalf("orphan retry stop = %+v, want success once termination confirmed", retry)
	}
	if d.inst.stopCount() != 2 {
		t.Fatalf("driver Stop called %d times, want exactly 2 (first fail + retry); the re-sent stop must not have taken the orphan", d.inst.stopCount())
	}
	// The retry released the reservation on success: the id is now genuinely unknown.
	after := m.Handle(context.Background(), session.Command{CommandID: "stop4", ServerID: "s1", Kind: "StopServer"})
	if after.Success || after.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("stop after orphan retry confirmed = %+v, want SERVER_NOT_FOUND", after)
	}
}

// A RestartServer whose recorded driver is no longer offered by this Worker must
// fail WITHOUT evicting the live running instance: the driver/launch-mode
// resolution happens after takeRunningReserve (issue #1619) but on failure the
// instance is restored so the still-running process stays tracked and reachable.
func TestRestartUnavailableDriverLeavesInstanceTracked(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{})
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}

	// Drop the driver the recorded StartServer used so the restart's resolution fails.
	delete(m.drivers, "container")

	res := m.Handle(context.Background(), session.Command{CommandID: "r", ServerID: "s1", Kind: "RestartServer"})
	if res.Success || res.ErrorCode != session.CommandErrorDriverUnavailable {
		t.Fatalf("restart with unavailable driver = %+v, want DRIVER_UNAVAILABLE", res)
	}

	// The instance must still be tracked and live: a ServerCommand reaches it (a
	// running instance), proving the failed restart restored it after eviction.
	if sc := m.Handle(context.Background(), session.Command{CommandID: "c", ServerID: "s1", Kind: "ServerCommand", Line: "list"}); sc.ErrorCode == session.CommandErrorServerNotFound {
		t.Fatalf("ServerCommand after failed restart = %+v, want the instance still tracked (not evicted)", sc)
	}
	// And the id must not be left reserved: restore the driver and confirm a stop
	// still cleanly terminates the still-tracked instance.
	m.drivers["container"] = d
	if stop := m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: "s1", Kind: "StopServer"}); !stop.Success {
		t.Fatalf("stop after failed restart = %+v, want success (instance was still tracked, id not wedged)", stop)
	}
}

// A RestartServer for an unknown id must return SERVER_NOT_FOUND without
// leaking a reservation: a subsequent StartServer for the same id must succeed,
// not be wedged with BUSY. The pre-fix code's hasRunning pre-check raced with
// takeRunningReserve and permanently leaked reserved[id]=true on the takeFound
// path (issue #1950).
func TestRestartNeverLeaksReservation(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)

	// Restart for an id that was never started: must return SERVER_NOT_FOUND.
	res := m.Handle(context.Background(), session.Command{CommandID: "r1", ServerID: "s1", Kind: "RestartServer"})
	if res.Success || res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("restart unknown id = %+v, want SERVER_NOT_FOUND", res)
	}

	// The reserved map must be empty: no leaked reservation.
	m.mu.Lock()
	leaked := m.reserved["s1"]
	m.mu.Unlock()
	if leaked {
		t.Fatal("reserved[s1] leaked after restart of unknown id (issue #1950)")
	}

	// The id must not be leaked as reserved: a StartServer must succeed (not BUSY).
	start := m.Handle(context.Background(), startCmd())
	if !start.Success {
		t.Fatalf("start after restart of unknown id = %+v, want success (reservation must not leak)", start)
	}
}

// A RestartServer dispatched while a StartServer is still mid-driver.Start must
// return BUSY and not permanently wedge the id: after the start completes, a
// StopServer must succeed (the reservation was never leaked). Issue #1950.
func TestRestartRacingStartCompletion(t *testing.T) {
	d := newGatedDriver()
	m := newManager(t, d, nil)

	// Start a server but hold it inside driver.Start.
	startDone := make(chan session.CommandResult, 1)
	go func() { startDone <- m.Handle(context.Background(), startCmd()) }()
	awaitEnter(t, d.entered)

	// Restart while the start is in flight: must return BUSY.
	res := m.Handle(context.Background(), session.Command{CommandID: "r1", ServerID: "s1", Kind: "RestartServer"})
	if res.Success || res.ErrorCode != session.CommandErrorBusy {
		t.Fatalf("restart during in-flight start = %+v, want BUSY", res)
	}

	// Release the start; it must succeed.
	close(d.release)
	sr := <-startDone
	if !sr.Success {
		t.Fatalf("start = %+v, want success", sr)
	}

	// The id must not be permanently wedged: a StopServer must succeed.
	stop := m.Handle(context.Background(), session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})
	if !stop.Success {
		t.Fatalf("stop after restart+start completion = %+v, want success (no permanent BUSY leak)", stop)
	}
}

// A RestartServer dispatched while a StartServer holds the reservation (mid-
// driver.Start) must return BUSY. Issue #1950.
func TestRestartMidStartStillBusy(t *testing.T) {
	d := newGatedDriver()
	m := newManager(t, d, nil)

	// Hold a StartServer mid-driver.Start.
	go func() { _ = m.Handle(context.Background(), startCmd()) }()
	awaitEnter(t, d.entered)

	// Restart while reservation is held: BUSY expected.
	res := m.Handle(context.Background(), session.Command{CommandID: "r1", ServerID: "s1", Kind: "RestartServer"})
	if res.Success || res.ErrorCode != session.CommandErrorBusy {
		t.Fatalf("restart mid-start = %+v, want BUSY", res)
	}

	close(d.release)
}

// Concurrent StartServer + RestartServer on the same id must never permanently
// leak a reservation. The pre-fix TOCTOU window between hasRunning (which
// released mu) and the inner takeRunningReserve allowed a concurrent start to
// commit the instance between the two calls, causing takeFound whose evicted
// instance was discarded without release — permanently wedging the id.
//
// This stress test opens that window by hammering concurrent start+restart
// pairs over many unique ids. Under the old code it reliably leaks within a few
// thousand iterations (~0.5s); under the fix it passes deterministically.
func TestRestartConcurrentStartNeverLeaksReservation(t *testing.T) {
	const iterations = 30000
	d := &fakeDriver{}
	m := newManager(t, d, nil)

	for i := 0; i < iterations; i++ {
		id := fmt.Sprintf("srv-%d", i)
		var wg sync.WaitGroup
		wg.Add(2)

		go func() {
			defer wg.Done()
			m.Handle(context.Background(), session.Command{
				CommandID: "start-" + id, ServerID: id, Kind: "StartServer",
				Driver: "container", MinecraftVersion: "1.21",
			})
		}()

		go func() {
			defer wg.Done()
			m.Handle(context.Background(), session.Command{
				CommandID: "restart-" + id, ServerID: id, Kind: "RestartServer",
			})
		}()

		wg.Wait()

		// The id must not be leaked as reserved: a follow-up StartServer for the
		// same id must never return BUSY. Under the pre-fix race, takeFound inside
		// the hasRunning-false branch discarded the evicted instance and returned
		// SERVER_NOT_FOUND without calling release(), permanently leaking
		// reserved[id]=true.
		m.mu.Lock()
		leaked := m.reserved[id]
		m.mu.Unlock()
		if leaked {
			t.Fatalf("iteration %d: reserved[%s] leaked after concurrent start+restart (issue #1950)", i, id)
		}
	}
}
