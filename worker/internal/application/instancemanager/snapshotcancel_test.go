package instancemanager

import (
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// A snapshot whose session ends before it has finished — the stream dropped, or
// the Worker is shutting down — is reported as cancelled (issue #3340), not as a
// snapshot abandoned at its deadline nor as a failed pack: no deadline passed,
// and the answer reaches no caller, since the session sends nothing once it has
// ended. That holds whether the command answers while its work is still blocked
// on storage, or its work answers first with the error the cancellation caused.
func TestASnapshotWhoseSessionEndsIsReportedAsCancelled(t *testing.T) {
	for _, tc := range []struct {
		name string
		// workAnswersFirst ends the session from inside the transfer, under a
		// command deadline the session's end does not reach, so the work's own
		// failure is what the command receives. Otherwise the transfer stays
		// blocked until the test releases it, as on stuck storage.
		workAnswersFirst bool
	}{
		{"while the work is blocked on storage", false},
		{"after the work answered", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sessionCtx, endSession := context.WithCancel(context.Background())
			defer endSession()
			st := newStall(t)
			// Once released, the transfer returns the cancellation's error, as the
			// pack does once a blocked call returns.
			tr := &fakeTransfer{stallSnapshot: st.wait, err: context.Canceled}
			h := &syncSlogHandler{}
			m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr).WithLogger(slog.New(h))
			if tc.workAnswersFirst {
				tr.stallSnapshot = endSession
				m.snapshotContextHook = func(context.Context, bool) (context.Context, context.CancelFunc) {
					return context.WithCancel(context.Background())
				}
			}
			seedScratch(t, m, s1)
			exited := awaitSnapshotReleased(m)

			done := make(chan session.CommandResult, 1)
			go func() { done <- m.Handle(sessionCtx, snapshotCmd()) }()
			var res session.CommandResult
			if tc.workAnswersFirst {
				// Nothing is held: the answer waits on every phase's filesystem work.
				res = awaitCompleted(t, done)
			} else {
				awaitEnter(t, st.entered)
				endSession()
				res = awaitAnswer(t, done)
			}
			st.unblock()
			// The released transfer still lists the working dir (hasWorkingSet).
			awaitFinished(t, exited, 120*time.Second)

			if res.Success || !strings.Contains(res.ErrorMessage, "cancelled") || strings.Contains(res.ErrorMessage, "deadline") {
				t.Fatalf("SnapshotTrigger whose session ended = %+v, want it reported as cancelled", res)
			}
			for _, r := range h.snapshot() {
				if r.Level >= slog.LevelWarn {
					t.Errorf("a cancelled snapshot logged %s %q", r.Level, r.Message)
				}
			}
		})
	}
}

// The other side of that split: a snapshot whose deadline passes while its
// session is alive is still a failed snapshot (issue #3511). It answers
// "abandoned at its deadline" and WARNs, because the API receives that answer
// and the world was not captured.
func TestASnapshotPastItsDeadlineIsStillReportedAsAbandoned(t *testing.T) {
	st := newStall(t)
	h := &syncSlogHandler{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{stallSnapshot: st.wait}).WithLogger(slog.New(h))
	seedScratch(t, m, s1)
	exited := awaitSnapshotReleased(m)
	expire := snapshotExpiry(t, m)

	done := goHandle(m, snapshotCmd())
	awaitEnter(t, st.entered)
	expire()
	res := awaitAnswer(t, done)
	st.unblock()
	// The released transfer still lists the working dir (hasWorkingSet).
	awaitFinished(t, exited, 120*time.Second)

	assertFailed(t, res)
	if !strings.Contains(res.ErrorMessage, "abandoned at its deadline") {
		t.Fatalf("SnapshotTrigger past its deadline = %+v, want it abandoned at its deadline", res)
	}
	warned := false
	for _, r := range h.snapshot() {
		if r.Level == slog.LevelWarn && strings.Contains(r.Message, "abandoned at its deadline") {
			warned = true
		}
	}
	if !warned {
		t.Fatal("a snapshot abandoned at its deadline logged no WARN")
	}
}

// The cancelled snapshot logs the work's own message capped at 1 KiB, on a rune
// boundary and marked as truncated, as the session caps the error it logs: an
// over-limit pack error quotes a path of about 4 KiB. A shorter message is logged
// whole. The long fixture puts the cap inside a rune: after the leading "a", byte
// 1024 is the second byte of an "é", so a cut that did not back off to the rune's
// start would log half of it.
func TestACancelledSnapshotCapsTheDetailItLogs(t *testing.T) {
	for _, detail := range []string{"short", "a" + strings.Repeat("é", 1000)} {
		h := &syncSlogHandler{}
		m := newManager(t, &fakeDriver{}, nil).WithLogger(slog.New(h))
		m.snapshotCancelled(snapshotCmd(), detail)

		var logged string
		for _, r := range h.snapshot() {
			r.Attrs(func(a slog.Attr) bool {
				if a.Key == "detail" {
					logged = a.Value.String()
				}
				return true
			})
		}
		if len(detail) <= 1024 {
			if logged != detail {
				t.Errorf("detail logged as %q, want %q", logged, detail)
			}
			continue
		}
		kept, cut := strings.CutSuffix(logged, "…[truncated]")
		if !cut || len(kept) > 1024 || !utf8.ValidString(kept) || !strings.HasPrefix(detail, kept) {
			t.Errorf("a %d-byte detail was logged as %d bytes (%q…), want at most 1024 bytes of it and the truncation marker", len(detail), len(logged), logged[:min(len(logged), 20)])
		}
	}
}
