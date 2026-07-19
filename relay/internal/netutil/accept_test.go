package netutil

import (
	"context"
	"errors"
	"net"
	"syscall"
	"testing"
	"time"
)

func TestIsTransientAcceptError(t *testing.T) {
	tests := []struct {
		name string
		err  error
		want bool
	}{
		{"EMFILE", syscall.EMFILE, true},
		{"ENFILE", syscall.ENFILE, true},
		{"ENOBUFS", syscall.ENOBUFS, true},
		{"ENOMEM", syscall.ENOMEM, true},
		{"wrapped EMFILE", &net.OpError{Op: "accept", Err: syscall.EMFILE}, true},
		{"net.ErrClosed", net.ErrClosed, false},
		{"generic error", errors.New("connection refused"), false},
		{"nil", nil, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := IsTransientAcceptError(tt.err)
			if got != tt.want {
				t.Errorf("IsTransientAcceptError(%v) = %v, want %v", tt.err, got, tt.want)
			}
		})
	}
}

func TestAcceptBackoffProgression(t *testing.T) {
	var b AcceptBackoff

	want := []time.Duration{
		5 * time.Millisecond,
		10 * time.Millisecond,
		20 * time.Millisecond,
		40 * time.Millisecond,
		80 * time.Millisecond,
		160 * time.Millisecond,
		320 * time.Millisecond,
		640 * time.Millisecond,
		1000 * time.Millisecond,
		1000 * time.Millisecond, // capped
	}
	for i, w := range want {
		got := b.Next()
		if got != w {
			t.Errorf("step %d: got %v, want %v", i, got, w)
		}
	}
}

func TestAcceptBackoffReset(t *testing.T) {
	var b AcceptBackoff
	b.Next() // 5ms
	b.Next() // 10ms
	b.Reset()
	if got := b.Next(); got != 5*time.Millisecond {
		t.Errorf("after Reset: got %v, want 5ms", got)
	}
}

func TestAcceptBackoffSleepCompletes(t *testing.T) {
	var b AcceptBackoff
	ctx := context.Background()
	start := time.Now()
	if !b.Sleep(ctx) {
		t.Error("Sleep returned false on non-cancelled ctx")
	}
	elapsed := time.Since(start)
	if elapsed < 4*time.Millisecond {
		t.Errorf("Sleep returned too quickly: %v", elapsed)
	}
}

func TestAcceptBackoffSleepCancelled(t *testing.T) {
	var b AcceptBackoff
	// Advance backoff to 1s so we can tell if cancel works quickly.
	for i := 0; i < 10; i++ {
		b.Next()
	}
	b.Reset()
	// Set a long backoff by manually advancing.
	for i := 0; i < 9; i++ {
		b.Next()
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // cancel immediately

	if b.Sleep(ctx) {
		t.Error("Sleep returned true on already-cancelled ctx")
	}
}
