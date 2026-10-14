package execution

import (
	"context"
	"errors"
	"testing"
)

func TestWorkingSetLockExcludesUntilUnlocked(t *testing.T) {
	l := NewWorkingSetLock()
	if err := l.Lock(context.Background()); err != nil {
		t.Fatalf("Lock on a free lock: %v", err)
	}
	if l.TryLock() {
		t.Fatal("TryLock took a held lock")
	}
	l.Unlock()
	if !l.TryLock() {
		t.Fatal("TryLock did not take a released lock")
	}
}

// A waiter is handed the lock when the holder releases it.
func TestWorkingSetLockHandsOverToAWaiter(t *testing.T) {
	l := NewWorkingSetLock()
	if !l.TryLock() {
		t.Fatal("TryLock on a free lock failed")
	}
	got := make(chan error, 1)
	go func() { got <- l.Lock(context.Background()) }()
	l.Unlock()
	if err := <-got; err != nil {
		t.Fatalf("waiter's Lock = %v, want the lock", err)
	}
	if l.TryLock() {
		t.Fatal("the lock is free although the waiter took it")
	}
}

// A waiter whose context ends leaves without the lock, which stays the holder's.
func TestWorkingSetLockWaitEndsWithTheContext(t *testing.T) {
	l := NewWorkingSetLock()
	if !l.TryLock() {
		t.Fatal("TryLock on a free lock failed")
	}
	ctx, cancel := context.WithCancel(context.Background())
	got := make(chan error, 1)
	go func() { got <- l.Lock(ctx) }()
	cancel()
	if err := <-got; !errors.Is(err, context.Canceled) {
		t.Fatalf("waiter's Lock = %v, want context.Canceled", err)
	}
	if l.TryLock() {
		t.Fatal("the lock came free when a waiter gave up")
	}
	l.Unlock()
	if !l.TryLock() {
		t.Fatal("the lock is not free after its holder released it")
	}
}

// A context that is already done never gets the lock, even a free one: whoever
// asked is no longer there to use it.
func TestWorkingSetLockRefusesADoneContext(t *testing.T) {
	l := NewWorkingSetLock()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for range 100 {
		if err := l.Lock(ctx); !errors.Is(err, context.Canceled) {
			t.Fatalf("Lock with a done context = %v, want context.Canceled", err)
		}
	}
	if !l.TryLock() {
		t.Fatal("a refused Lock left the lock held")
	}
}
