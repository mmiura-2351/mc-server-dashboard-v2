package session

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
)

type fakeReportClient struct {
	mu     sync.Mutex
	starts []apiclient.SessionStart
	ends   []apiclient.SessionEnd
	failN  int // fail the next N calls
}

func (f *fakeReportClient) ReportSessions(_ context.Context, starts []apiclient.SessionStart, ends []apiclient.SessionEnd) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failN > 0 {
		f.failN--
		return errors.New("transient")
	}
	f.starts = append(f.starts, starts...)
	f.ends = append(f.ends, ends...)
	return nil
}

func (f *fakeReportClient) counts() (int, int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.starts), len(f.ends)
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestReporterStartEndTracksActive(t *testing.T) {
	r := NewReporter(&fakeReportClient{}, discardLogger(), func() time.Time { return time.Unix(0, 0) })

	id := r.Start("srv", "amber", "1.2.3.4", "Steve", "uuid")
	if got := r.ActiveSessionIDs(); len(got) != 1 || got[0] != id {
		t.Fatalf("active = %v, want [%s]", got, id)
	}
	r.End(id)
	if got := r.ActiveSessionIDs(); len(got) != 0 {
		t.Fatalf("active after End = %v, want empty", got)
	}
}

func TestReporterFlushDeliversBatch(t *testing.T) {
	fake := &fakeReportClient{}
	r := NewReporter(fake, discardLogger(), nil)
	id := r.Start("srv", "amber", "1.2.3.4", "Steve", "")
	r.End(id)

	r.flush(context.Background())
	starts, ends := fake.counts()
	if starts != 1 || ends != 1 {
		t.Errorf("delivered %d starts / %d ends, want 1/1", starts, ends)
	}
}

func TestReporterRetriesOnError(t *testing.T) {
	fake := &fakeReportClient{failN: 1}
	r := NewReporter(fake, discardLogger(), nil)
	r.Start("srv", "amber", "1.2.3.4", "Steve", "")

	// First flush fails; events are restored.
	r.flush(context.Background())
	if s, _ := fake.counts(); s != 0 {
		t.Fatalf("failed flush should deliver nothing, got %d", s)
	}
	// Second flush succeeds with the retained event.
	r.flush(context.Background())
	if s, _ := fake.counts(); s != 1 {
		t.Errorf("retry should deliver the retained event, got %d", s)
	}
}

func TestReporterRunFlushesOnShutdown(t *testing.T) {
	fake := &fakeReportClient{}
	r := NewReporter(fake, discardLogger(), nil)
	r.Start("srv", "amber", "1.2.3.4", "Steve", "")

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { r.Run(ctx); close(done) }()
	cancel()
	<-done

	if s, _ := fake.counts(); s != 1 {
		t.Errorf("shutdown flush should deliver the buffered start, got %d", s)
	}
}
