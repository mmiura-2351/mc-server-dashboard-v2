package instancemanager

import (
	"context"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// These tests pin the settle-wait's caps (issue #3503): the stop flush's is the
// time its context has left, less a margin, and the snapshot quiesce's is 180 s.
// They run the real caps on a clock only the test moves, so minutes of settle
// pass at once.

// settleClock is a manager clock whose time moves only when a settle scan
// advances it. After is the wall clock's, for the manager's other waiters (the
// metrics pump), which these tests do not drive.
type settleClock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *settleClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *settleClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

func (c *settleClock) After(d time.Duration) <-chan time.Time { return time.After(d) }

func (c *settleClock) NewTimer(time.Duration) session.Timer { return noopTimer{} }

// onSettleClock runs m's settle-wait on a clock started at the wall clock's now,
// with one production poll interval passing before every scan but the first, as
// in the real wait. The world is still being written until settleAfter has passed
// on that clock, and unchanged from then on. It returns the clock and the number
// of scans made.
func onSettleClock(m *Manager, settleAfter time.Duration) (*settleClock, *int) {
	clk := &settleClock{now: time.Now()}
	start := clk.Now()
	m.clock = clk
	m.settlePollInterval = 0
	scans := new(int)
	m.scanRegion = func(string) (regionState, error) {
		if *scans > 0 {
			clk.advance(defaultSettlePollInterval)
		}
		*scans++
		size := int64(-1) // the settled world
		if clk.Now().Sub(start) < settleAfter {
			size = int64(*scans) // still being written: a new size every scan
		}
		return regionState{"r.0.0.mca": {modTime: time.Unix(0, 0), size: size}}, nil
	}
	return clk, scans
}

// never is a settleAfter no settle-wait reaches.
const never = time.Duration(1<<63 - 1)

// clockDeadline is a context whose deadline is on the settle clock and which never
// expires by itself, so the settle-wait can only end on its own cap: a context that
// fired first would hide a cap that outlived it.
type clockDeadline struct {
	context.Context
	deadline time.Time
}

func (c clockDeadline) Deadline() (time.Time, bool) { return c.deadline, true }

// loggedSettleBudget returns the budget the settle timeout WARN of caller logged,
// and whether there was one.
func loggedSettleBudget(t *testing.T, records []slog.Record, caller string) (time.Duration, bool) {
	t.Helper()
	for _, r := range records {
		if r.Level != slog.LevelWarn || !strings.HasPrefix(r.Message, caller+": working set did not settle") {
			continue
		}
		var budget time.Duration
		found := false
		r.Attrs(func(a slog.Attr) bool {
			if a.Key == "budget" {
				budget, found = a.Value.Duration(), true
				return false
			}
			return true
		})
		if !found {
			t.Fatalf("settle timeout WARN %q carries no budget", r.Message)
		}
		return budget, true
	}
	return 0, false
}

// The stop flush runs under the driver's flush budget (defaultFlushTimeout, 90 s),
// which also pays for the dial, save-off and save-all before the settle starts. Its
// settle may use what that budget has left, less flushSettleMargin, and must give
// up before the context does: a fixed 60 s cap wasted the rest of a slow disk's
// save time, and a cap equal to the time left would be cut by the context.
func TestStopFlushSettleCapIsTheTimeItsContextHasLeft(t *testing.T) {
	for _, left := range []time.Duration{85 * time.Second, 10 * time.Second} {
		t.Run(left.String(), func(t *testing.T) {
			h := &syncSlogHandler{}
			m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithLogger(slog.New(h))
			clk, _ := onSettleClock(m, never)
			ctx := clockDeadline{Context: context.Background(), deadline: clk.Now().Add(left)}

			if m.flushBeforeStopWithDriver(ctx, s1, "container", "") {
				t.Fatal("flush = true for a world that never settled")
			}

			budget, ok := loggedSettleBudget(t, h.snapshot(), settleForStop)
			if !ok {
				t.Fatal("no stop flush settle timeout WARN")
			}
			if want := left - flushSettleMargin; budget != want {
				t.Fatalf("settle budget = %v with %v left, want %v (the time left less the margin)", budget, left, want)
			}
			if gaveUp := clk.Now(); gaveUp.After(ctx.deadline) {
				t.Fatalf("settle gave up %v past its context's deadline", gaveUp.Sub(ctx.deadline))
			}
		})
	}
}

// A slow disk's save can take minutes (85-143 s for a small vanilla world, issue
// #3436). The snapshot quiesce waits up to 180 s for it, so a save past the old
// 60 s budget is still taken.
func TestSnapshotQuiesceTakesASaveThatSettlesWithin180Seconds(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(tr)
	m.fsckRetryDelay = 0
	seedWorkingSet(t, m, s1, healthyRegion())
	onSettleClock(m, 170*time.Second)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success for a save that settled after 170 s", res)
	}
	if len(tr.packs) != 1 {
		t.Fatalf("packs = %v, want one", tr.packs)
	}
}

// A save that has not settled after 180 s refuses the snapshot quiesce_unavailable,
// and only once the 180 s are up.
func TestSnapshotQuiesceRefusesASaveStillWritingAfter180Seconds(t *testing.T) {
	h := &syncSlogHandler{}
	ctrl := &fakeControl{reply: "ok"}
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr).WithLogger(slog.New(h))
	seedScratch(t, m, s1)
	clk, _ := onSettleClock(m, never)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	start := clk.Now()

	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || !strings.Contains(res.ErrorMessage, "quiesce_unavailable") {
		t.Fatalf("SnapshotTrigger = %+v, want a quiesce_unavailable refusal", res)
	}
	if len(tr.snapshots) != 0 {
		t.Fatalf("snapshots = %v, want none for an unsettled world", tr.snapshots)
	}
	budget, ok := loggedSettleBudget(t, h.snapshot(), settleForSnapshot)
	if !ok {
		t.Fatal("no snapshot quiesce settle timeout WARN")
	}
	if budget != 180*time.Second {
		t.Fatalf("settle budget = %v, want 180s", budget)
	}
	if waited := clk.Now().Sub(start); waited < 180*time.Second {
		t.Fatalf("refused after %v, want the full 180 s waited", waited)
	}
}

// A save that is already on disk settles on the first poll, whatever the cap: two
// scans, one poll interval, for both callers.
func TestFastSettleReturnsAtOnce(t *testing.T) {
	for _, tc := range []struct {
		caller string
		run    func(t *testing.T, m *Manager)
	}{
		{settleForStop, func(t *testing.T, m *Manager) {
			ctx := clockDeadline{Context: context.Background(), deadline: m.clock.Now().Add(85 * time.Second)}
			if !m.flushBeforeStopWithDriver(ctx, s1, "container", "") {
				t.Fatal("flush = false for a settled world")
			}
		}},
		{settleForSnapshot, func(t *testing.T, m *Manager) {
			if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
				t.Fatalf("SnapshotTrigger = %+v, want success for a settled world", res)
			}
		}},
	} {
		t.Run(tc.caller, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{})
			m.fsckRetryDelay = 0
			seedWorkingSet(t, m, s1, healthyRegion())
			clk, scans := onSettleClock(m, 0)
			if res := m.Handle(context.Background(), startCmd()); !res.Success {
				t.Fatalf("seed running instance: %+v", res)
			}
			start := clk.Now()

			tc.run(t, m)

			if *scans != 2 {
				t.Fatalf("scans = %d, want 2 (settled on the first poll)", *scans)
			}
			if waited := clk.Now().Sub(start); waited != defaultSettlePollInterval {
				t.Fatalf("settled after %v, want one poll interval (%v)", waited, defaultSettlePollInterval)
			}
		})
	}
}
