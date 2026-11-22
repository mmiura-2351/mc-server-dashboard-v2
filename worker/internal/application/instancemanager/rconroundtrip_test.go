package instancemanager

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"maps"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"golang.org/x/sys/unix"
)

// These tests pin the RCON round-trip durations that the quiesce's and the stop
// flush's log lines carry (issue #4038), so how close a live reply comes to rcon's
// 30 s per-leg ceiling can be read off the Worker's log. Each command takes a set
// time on a manager clock that only the test moves and that the wall clock does
// not follow, and each dial takes dialTime on it, which no round trip includes.

const dialTime = 7 * time.Second

// answering is how long each command takes on a connection that answers it.
var answering = map[string]time.Duration{
	"save-off": 1200 * time.Millisecond,
	"save-all": 3400 * time.Millisecond,
	"save-on":  560 * time.Millisecond,
}

// clockedControl is an RCON connection whose command line takes delay[line] on
// clk, or answering[line] when delay has none. A line in fail fails after that
// time and, as rcon.Client does, poisons the connection, so every later command
// fails at once with ErrConnBroken.
type clockedControl struct {
	clk    *settleClock
	delay  map[string]time.Duration
	fail   map[string]bool
	broken bool
}

func (c *clockedControl) Execute(_ context.Context, line string) (string, error) {
	if c.broken {
		return "", execution.ErrConnBroken
	}
	d, ok := c.delay[line]
	if !ok {
		d = answering[line]
	}
	c.clk.advance(d)
	if c.fail[line] {
		c.broken = true
		return "", errors.New("test: i/o timeout")
	}
	return "", nil
}

func (c *clockedControl) Close() error { return nil }

// failsAfter is a connection on which line fails after d.
func failsAfter(line string, d time.Duration) *clockedControl {
	return &clockedControl{delay: map[string]time.Duration{line: d}, fail: map[string]bool{line: true}}
}

// dialing makes m's RCON dials hand out conns in order, each after dialTime on
// clk, and fail once they are used up.
func dialing(m *Manager, clk *settleClock, conns []*clockedControl) {
	dials := make(chan execution.ServerControl, len(conns))
	for _, c := range conns {
		c.clk = clk
		dials <- c
	}
	m.openControl = func(context.Context, string, string, string) (execution.ServerControl, error) {
		clk.advance(dialTime)
		select {
		case c := <-dials:
			return c, nil
		default:
			return nil, errors.New("test: no RCON connection left to dial")
		}
	}
}

// jsonLog collects what a JSON handler writes, so a test reads the fields as the
// Worker's log shows them.
type jsonLog struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (l *jsonLog) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.Write(p)
}

// roundTrips returns the round-trip fields of the first line whose message is msg.
func (l *jsonLog) roundTrips(t *testing.T, msg string) map[string]int64 {
	t.Helper()
	l.mu.Lock()
	defer l.mu.Unlock()
	for line := range bytes.Lines(l.buf.Bytes()) {
		var rec map[string]any
		if err := json.Unmarshal(line, &rec); err != nil {
			t.Fatalf("log line %q: %v", line, err)
		}
		if rec["msg"] != msg {
			continue
		}
		got := map[string]int64{}
		for _, key := range []string{"save_off_ms", "save_all_ms", "save_on_ms"} {
			if v, ok := rec[key].(float64); ok {
				got[key] = int64(v)
			}
		}
		return got
	}
	t.Fatalf("no %q line; log:\n%s", msg, l.buf.String())
	return nil
}

// roundTripCase is one run of the quiesce or the flush: the connections its dials
// get, whether the world settles, and the round-trip fields each named line must
// carry, exactly: a field that is absent is a command that did not go out.
type roundTripCase struct {
	name  string
	conns []*clockedControl
	// settleAfter is when the world stops being written; never times the settle out.
	settleAfter time.Duration
	// syncErr is what the snapshot's syncfs answers.
	syncErr error
	// retryDelay is the save-on restore's back-off before it redials.
	retryDelay time.Duration
	want       map[string]map[string]int64
}

func (tc roundTripCase) check(t *testing.T, log *jsonLog) {
	t.Helper()
	for msg, want := range tc.want {
		if got := log.roundTrips(t, msg); !maps.Equal(got, want) {
			t.Errorf("%q carries %v, want %v", msg, got, want)
		}
	}
}

const (
	quiesceRefused  = "snapshot refused: could not quiesce running world"
	snapshotDurable = "snapshot made the working set durable"
)

func TestQuiesceLogsItsRCONRoundTrips(t *testing.T) {
	for _, tc := range []roundTripCase{
		{
			name:  "a clean snapshot",
			conns: []*clockedControl{{}},
			want: map[string]map[string]int64{
				snapshotDurable: {"save_off_ms": 1200, "save_all_ms": 3400, "save_on_ms": 560},
			},
		},
		{
			name:    "a snapshot whose tree could not be made durable",
			conns:   []*clockedControl{{}},
			syncErr: unix.EIO,
			want: map[string]map[string]int64{
				"snapshot could not make the working set durable; it is published, but the generation marker is not advanced": {
					"save_off_ms": 1200, "save_all_ms": 3400, "save_on_ms": 560,
				},
			},
		},
		{
			name:  "a save-off that fails",
			conns: []*clockedControl{failsAfter("save-off", 30*time.Second)},
			want: map[string]map[string]int64{
				"snapshot save-off failed; snapshot will not be quiesced": {"save_off_ms": 30000},
				quiesceRefused: {"save_off_ms": 30000},
			},
		},
		{
			name:  "a save-all that fails",
			conns: []*clockedControl{failsAfter("save-all", 30*time.Second), {}},
			want: map[string]map[string]int64{
				"snapshot save-all failed": {"save_off_ms": 1200, "save_all_ms": 30000},
				quiesceRefused:             {"save_off_ms": 1200, "save_all_ms": 30000},
			},
		},
		{
			name:        "a save that never settles",
			conns:       []*clockedControl{{}},
			settleAfter: never,
			want: map[string]map[string]int64{
				quiesceRefused: {"save_off_ms": 1200, "save_all_ms": 3400},
			},
		},
		{
			name: "a save-on that fails and then lands on a redial",
			conns: []*clockedControl{
				failsAfter("save-on", 5*time.Second),
				{delay: map[string]time.Duration{"save-on": 700 * time.Millisecond}},
			},
			want: map[string]map[string]int64{
				"snapshot save-on failed on the quiesce connection; redialing": {
					"save_off_ms": 1200, "save_all_ms": 3400, "save_on_ms": 5000,
				},
				snapshotDurable: {"save_off_ms": 1200, "save_all_ms": 3400, "save_on_ms": 700},
			},
		},
		{
			name:  "a save-on that fails on the redial too",
			conns: []*clockedControl{failsAfter("save-on", 5*time.Second), failsAfter("save-on", 30*time.Second)},
			want: map[string]map[string]int64{
				"snapshot save-on NOT restored after redial; server left with auto-save disabled": {
					"save_off_ms": 1200, "save_all_ms": 3400, "save_on_ms": 30000,
				},
			},
		},
		{
			name:  "a save-on whose redial fails",
			conns: []*clockedControl{failsAfter("save-on", 5*time.Second)},
			want: map[string]map[string]int64{
				"snapshot save-on NOT restored; redial failed, server left with auto-save disabled": {
					"save_off_ms": 1200, "save_all_ms": 3400, "save_on_ms": 5000,
				},
			},
		},
		{
			name:       "a save-on whose restore runs out before the redial",
			conns:      []*clockedControl{failsAfter("save-on", 5*time.Second)},
			retryDelay: time.Hour,
			want: map[string]map[string]int64{
				"snapshot save-on NOT restored; server left with auto-save disabled": {
					"save_off_ms": 1200, "save_all_ms": 3400, "save_on_ms": 5000,
				},
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// The bubble ends the restore's own timeout without a wait.
			synctest.Test(t, func(t *testing.T) {
				log := &jsonLog{}
				m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{}).
					WithLogger(slog.New(slog.NewJSONHandler(log, nil)))
				m.fsckRetryDelay = tc.retryDelay
				clk, _ := onSettleClock(m, tc.settleAfter)
				startRunning(t, m)
				dialing(m, clk, tc.conns)
				stubSyncfs(t, func(int) error { return tc.syncErr })

				m.Handle(context.Background(), snapshotCmd())

				tc.check(t, log)
			})
		})
	}
}

func TestStopFlushLogsItsRCONRoundTrips(t *testing.T) {
	const settleTimedOut = "stop flush: working set did not settle within budget; stopping anyway"
	for _, tc := range []roundTripCase{
		{
			name:  "a clean flush",
			conns: []*clockedControl{{}},
			want: map[string]map[string]int64{
				"stop flush: working set settled": {"save_off_ms": 1200, "save_all_ms": 3400},
			},
		},
		{
			name:        "a flush whose save never settles",
			conns:       []*clockedControl{{}},
			settleAfter: never,
			want: map[string]map[string]int64{
				settleTimedOut: {"save_off_ms": 1200, "save_all_ms": 3400},
			},
		},
		{
			// The save-all goes out on a redial, which is not part of its round trip.
			name:        "a save-off that fails",
			conns:       []*clockedControl{failsAfter("save-off", 30*time.Second), {}},
			settleAfter: never,
			want: map[string]map[string]int64{
				"stop flush: save-off failed; proceeding with save-all": {"save_off_ms": 30000},
				settleTimedOut: {"save_off_ms": 30000, "save_all_ms": 3400},
			},
		},
		{
			name:  "a save-all that fails",
			conns: []*clockedControl{failsAfter("save-all", 30*time.Second)},
			want: map[string]map[string]int64{
				"stop flush: save-all failed; stopping without a final save": {"save_off_ms": 1200, "save_all_ms": 30000},
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			log := &jsonLog{}
			m := newManager(t, &fakeDriver{}, nil).WithLogger(slog.New(slog.NewJSONHandler(log, nil)))
			clk, _ := onSettleClock(m, tc.settleAfter)
			dialing(m, clk, tc.conns)

			m.flushBeforeStopWithDriver(context.Background(), s1, "container", "")

			tc.check(t, log)
		})
	}
}
