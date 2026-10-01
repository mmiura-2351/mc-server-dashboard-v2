package instancemanager

import (
	"context"
	"errors"
	"log/slog"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// tornRegion is a region whose only chunk declares a sector-filling length that
// overruns the truncated file (truncated_chunk): genuinely torn under the single rule
// set (issue #927), not a live-format unpadded tail.
func tornRegion() []byte { return healthyRegion()[:3*fsckSector-10] }

// seedHeldSet writes a held working set for s1 with one region file and a durable
// generation marker of gen: what a stopped server's retained scratch looks like after
// its last declared publish (issue #2481).
func seedHeldSet(t *testing.T, m *Manager, region []byte, gen uint64) string {
	t.Helper()
	seedWorkingSet(t, m, "s1", region)
	dir := filepath.Join(m.scratchDir, "s1")
	if err := writeGeneration(dir, gen); err != nil {
		t.Fatal(err)
	}
	return dir
}

// newQuiescedManager is a manager whose boot orphan sweep established quiescence (the
// production default when the sweep succeeds), so the launch fsck is allowed to judge.
func newQuiescedManager(t *testing.T, d *fakeDriver, h *syncSlogHandler) *Manager {
	t.Helper()
	m := newManager(t, d, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{}).WithQuiesced(true)
	if h != nil {
		m.WithLogger(slog.New(h))
	}
	return m
}

// assertTornRefusal checks a launch was refused as torn: SERVER_NOT_FOUND carrying the
// "working set torn" phrase the API keys its hydrate replay on — never the "working dir
// absent" one, which would log a scratch destroyed out of band (issue #3201).
func assertTornRefusal(t *testing.T, res session.CommandResult) {
	t.Helper()
	if res.Success {
		t.Fatalf("launch over a torn working set = %+v, want a refusal", res)
	}
	if res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("ErrorCode = %v, want %v", res.ErrorCode, session.CommandErrorServerNotFound)
	}
	if !strings.Contains(res.ErrorMessage, "working set torn") {
		t.Fatalf("refusal message = %q, want the API-pinned phrase %q", res.ErrorMessage, "working set torn")
	}
	if strings.Contains(res.ErrorMessage, "working dir absent") {
		t.Fatalf("refusal message = %q carries the working-set-absent phrase: the API would report "+
			"a scratch destroyed out of band", res.ErrorMessage)
	}
}

// A start the API sent WITHOUT a hydrate over a held set that is torn is refused before
// driver.Start (issue #3201). The shape: a stopped server's final snapshot was refused by
// the pre-pack fsck, so the torn scratch is retained, while the API's held inventory still
// carries the generation the Worker last declared on a publish (issue #2481) — so the next
// start skips the hydrate, and the marker-presence guard (issue #2802) passed it. The
// refusal sends the API's #2499 replay WITH the hydrate. The verdict is also persisted in
// the marker (as the boot scan does, issue #3178), so the next registration advertises 0
// rather than the generation the refusal just contradicted, and the WARN keeps the
// recorded generation for a manual recovery (STORAGE.md Section 4.6).
func TestStartRefusedWhenASkippedHydrateMeetsATornWorkingSet(t *testing.T) {
	d := &fakeDriver{}
	h := &syncSlogHandler{}
	m := newQuiescedManager(t, d, h)
	dir := seedHeldSet(t, m, tornRegion(), 9)

	res := m.Handle(context.Background(), startCmd())

	assertTornRefusal(t, res)
	if d.startCount() != 0 {
		t.Fatalf("driver started %d times, want 0: the torn world must not boot", d.startCount())
	}
	m.mu.Lock()
	leaked := m.reserved["s1"]
	m.mu.Unlock()
	if leaked {
		t.Fatal("reserved[s1] leaked after the torn refusal: the API's replay hydrate would be refused BUSY")
	}
	if gen := readGeneration(dir); gen != 0 {
		t.Errorf("marker = %d after the torn refusal, want 0: the next registration must agree with it", gen)
	}
	var logged bool
	for _, r := range h.snapshot() {
		if r.Level != slog.LevelWarn || !strings.Contains(r.Message, "launch refused") {
			continue
		}
		r.Attrs(func(a slog.Attr) bool {
			if a.Key == "recorded_generation" && a.Value.Kind() == slog.KindUint64 && a.Value.Uint64() == 9 {
				logged = true
			}
			return true
		})
	}
	if !logged {
		t.Errorf("no launch-refused WARN carrying recorded_generation=9; records: %v", h.snapshot())
	}

	// The replay: a hydrate replaces the tree (here: the fake swaps in a sound one), and
	// the start that follows it launches.
	seedHeldSet(t, m, healthyRegion(), 9)
	if hy := m.Handle(context.Background(), hydrateCmd()); !hy.Success {
		t.Fatalf("replay hydrate = %+v, want success", hy)
	}
	if start := m.Handle(context.Background(), startCmd()); !start.Success {
		t.Fatalf("replayed start = %+v, want success", start)
	}
}

// A start over a SOUND held set that skipped the hydrate launches, and the judgement
// leaves the marker exactly as it was (issue #3201).
func TestStartOverASoundSkippedHydrateSetIsUnaffected(t *testing.T) {
	for _, tc := range []struct {
		name   string
		region []byte
	}{
		{name: "aligned", region: healthyRegion()},
		{name: "live-format unpadded tail", region: unalignedLiveRegion()},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := &fakeDriver{}
			m := newQuiescedManager(t, d, nil)
			dir := seedHeldSet(t, m, tc.region, 9)
			before := readMarkerBytes(t, dir)

			if res := m.Handle(context.Background(), startCmd()); !res.Success {
				t.Fatalf("StartServer over a sound held set = %+v, want success", res)
			}
			if d.startCount() != 1 {
				t.Fatalf("driver started %d times, want 1", d.startCount())
			}
			if after := readMarkerBytes(t, dir); string(after) != string(before) {
				t.Errorf("marker = %q after the launch, want it byte-identical %q", after, before)
			}
		})
	}
}

// A start the API DID hydrate for is not judged (issue #3201). The tree is what the
// store served, so a torn verdict there has no replay to recover through: the only ways
// it can be torn are an operator's forced restore of a corrupt backup (STORAGE.md,
// restore_backup force=True) and a 204 "nothing published" hydrate, which leaves the
// held tree as the only copy there is. Refusing either would make the server unstartable.
// The exemption is ONE launch: the next start over the same tree without a hydrate in
// between is judged again.
func TestStartAfterAHydrateIsNotJudgedOnce(t *testing.T) {
	d := &fakeDriver{}
	m := newQuiescedManager(t, d, nil)
	dir := seedHeldSet(t, m, tornRegion(), 9)

	if hy := m.Handle(context.Background(), hydrateCmd()); !hy.Success {
		t.Fatalf("HydrateTrigger = %+v, want success", hy)
	}
	// The fake transfer leaves the tree as it was, as a 204 does; its marker write
	// records the fake's generation 0.
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer right after a hydrate = %+v, want success: a hydrated tree is not judged", res)
	}
	if stop := m.Handle(context.Background(), session.Command{CommandID: "s", ServerID: "s1", Kind: "StopServer"}); !stop.Success {
		t.Fatalf("StopServer = %+v, want success", stop)
	}
	if err := writeGeneration(dir, 9); err != nil {
		t.Fatal(err)
	}

	assertTornRefusal(t, m.Handle(context.Background(), startCmd()))
	if d.startCount() != 1 {
		t.Fatalf("driver started %d times, want 1: the second start skipped the hydrate and must be judged", d.startCount())
	}
}

// A FAILED hydrate exempts nothing: only a hydrate that succeeded replaced the tree.
func TestStartAfterAFailedHydrateIsJudged(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, &fakeControl{reply: "ok"}).
		WithTransfer(&fakeTransfer{err: errors.New("injected: transfer failed")}).WithQuiesced(true)
	seedHeldSet(t, m, tornRegion(), 9)

	if hy := m.Handle(context.Background(), hydrateCmd()); hy.Success {
		t.Fatalf("HydrateTrigger = %+v, want the injected failure", hy)
	}

	assertTornRefusal(t, m.Handle(context.Background(), startCmd()))
}

// The launch fsck does not run unless the boot orphan sweep established quiescence
// (issue #3171's rule, applied at launch). After a failed sweep an orphan this Worker
// does not track can still be running with the world bind-mounted; a torn-looking read
// of it would send the API's replay hydrate over a live world. Unjudged, the start
// reaches driver.Start, which declines a running container holding the name.
func TestStartIsNotJudgedWhenQuiescenceIsUnproven(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{})
	dir := seedHeldSet(t, m, tornRegion(), 9)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer with quiescence unproven = %+v, want it launched unjudged", res)
	}
	if gen := readGeneration(dir); gen != 9 {
		t.Errorf("marker = %d, want 9: an unjudged set's marker is not rewritten", gen)
	}
}

// A torn verdict whose marker rewrite fails still refuses the launch: the refusal is
// what keeps the torn world from booting, the rewrite only what makes the next
// registration agree with it.
func TestStartTornRefusalSurvivesAFailedMarkerRewrite(t *testing.T) {
	d := &fakeDriver{}
	h := &syncSlogHandler{}
	m := newQuiescedManager(t, d, h)
	dir := seedHeldSet(t, m, tornRegion(), 9)
	orig := rewriteTornMarker
	rewriteTornMarker = func(string, uint64) error { return errors.New("injected: disk full") }
	t.Cleanup(func() { rewriteTornMarker = orig })

	assertTornRefusal(t, m.Handle(context.Background(), startCmd()))
	if d.startCount() != 0 {
		t.Fatalf("driver started %d times, want 0", d.startCount())
	}
	if gen := readGeneration(dir); gen != 9 {
		t.Errorf("marker = %d, want 9 untouched by the failed rewrite", gen)
	}
	var logged bool
	for _, r := range h.snapshot() {
		if r.Level == slog.LevelWarn && strings.Contains(r.Message, "could not be rewritten") {
			logged = true
		}
	}
	if !logged {
		t.Errorf("no WARN reporting the failed marker rewrite; records: %v", h.snapshot())
	}
}

// A RESTART's relaunch is a launch no hydrate preceded, so it is judged too (issue
// #3201): a stop that had to be escalated to a kill can tear the world it stops. As with
// the working-set-absent relaunch (issue #2802), the stop is already taken, so the server
// is left down and evicted, unreserved for the reconciler's re-launch.
func TestRestartRelaunchRefusedOverATornWorkingSet(t *testing.T) {
	d := &fakeDriver{}
	m := newQuiescedManager(t, d, nil)
	startRunning(t, m)
	seedHeldSet(t, m, tornRegion(), 9)

	res := m.Handle(context.Background(), session.Command{CommandID: "r", ServerID: "s1", Kind: "RestartServer"})

	assertTornRefusal(t, res)
	if res.CommandID != "r" {
		t.Fatalf("CommandID = %q, want the RestartServer's own correlation id", res.CommandID)
	}
	if d.startCount() != 1 {
		t.Fatalf("driver started %d times, want 1 (the original start only)", d.startCount())
	}
	m.mu.Lock()
	_, tracked := m.instances["s1"]
	leaked := m.reserved["s1"]
	m.mu.Unlock()
	if tracked || leaked {
		t.Fatalf("after the refused relaunch: tracked=%v reserved=%v, want neither", tracked, leaked)
	}
}
