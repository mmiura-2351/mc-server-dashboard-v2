package instancemanager

import (
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

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
			if !tc.workAnswersFirst {
				awaitEnter(t, st.entered)
				endSession()
			}
			res := awaitAnswer(t, done)
			st.unblock()
			awaitFinished(t, exited, 10*time.Second)

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
