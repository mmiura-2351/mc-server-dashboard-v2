package session

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"time"
)

// discardLogger is a slog logger that writes nowhere, keeping test output clean.
func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// fakeClock is a manually-advanced Clock. After returns a channel a test fires
// by calling fire(); Now is fixed (tests that need it advance manually).
type fakeClock struct {
	mu      sync.Mutex
	now     time.Time
	pending []chan time.Time
}

func newFakeClock() *fakeClock {
	return &fakeClock{now: time.Unix(0, 0)}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) After(time.Duration) <-chan time.Time {
	ch := make(chan time.Time, 1)
	c.mu.Lock()
	c.pending = append(c.pending, ch)
	c.mu.Unlock()
	return ch
}

// fireNext fires the oldest pending timer, returning false if none is pending.
func (c *fakeClock) fireNext() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.pending) == 0 {
		return false
	}
	ch := c.pending[0]
	c.pending = c.pending[1:]
	ch <- c.now
	return true
}

// errStreamClosed simulates the server dropping the stream.
var errStreamClosed = errors.New("fake: stream closed")

// fakeTransport is one in-memory Session stream. Inbound commands are queued on
// commands; recorded outputs are captured for assertions.
type fakeTransport struct {
	mu sync.Mutex

	ack RegisterAck

	registers   int
	heartbeats  int
	results     []CommandResult
	closed      bool
	commands    chan Command
	recvErr     error // returned by RecvCommand once commands drains
	registerErr error // returned by SendRegister, if set
}

func newFakeTransport(ack RegisterAck) *fakeTransport {
	return &fakeTransport{
		ack:      ack,
		commands: make(chan Command, 8),
		recvErr:  errStreamClosed,
	}
}

func (t *fakeTransport) SendRegister(_ context.Context, _ Capabilities) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.registers++
	return t.registerErr
}

func (t *fakeTransport) RecvRegisterAck(_ context.Context) (RegisterAck, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.ack, nil
}

func (t *fakeTransport) SendHeartbeat(_ context.Context) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.heartbeats++
	return nil
}

func (t *fakeTransport) SendCommandResult(_ context.Context, result CommandResult) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.results = append(t.results, result)
	return nil
}

func (t *fakeTransport) RecvCommand(ctx context.Context) (Command, error) {
	select {
	case <-ctx.Done():
		return Command{}, ctx.Err()
	case cmd, ok := <-t.commands:
		if !ok {
			return Command{}, t.recvErr
		}
		return cmd, nil
	}
}

func (t *fakeTransport) Close() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.closed = true
	return nil
}

func (t *fakeTransport) heartbeatCount() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.heartbeats
}

func (t *fakeTransport) resultsCopy() []CommandResult {
	t.mu.Lock()
	defer t.mu.Unlock()
	return append([]CommandResult(nil), t.results...)
}

// fakeDialer hands out a queue of transports, one per Dial; once exhausted it
// returns dialErr (or the last transport repeatedly if loop is set).
type fakeDialer struct {
	mu         sync.Mutex
	transports []*fakeTransport
	calls      int
	dialErr    error
}

func (d *fakeDialer) Dial(_ context.Context) (Transport, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	idx := d.calls
	d.calls++
	if idx >= len(d.transports) {
		if d.dialErr != nil {
			return nil, d.dialErr
		}
		return nil, errStreamClosed
	}
	return d.transports[idx], nil
}

func (d *fakeDialer) dialCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.calls
}
