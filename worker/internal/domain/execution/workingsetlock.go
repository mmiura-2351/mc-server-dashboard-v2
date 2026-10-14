package execution

import "context"

// WorkingSetLock is one server's working-set lock (issue #3371). It is held by
// whoever, on the Worker's side, publishes a name in that server's working set
// by renaming a temporary one into place, for as long as the temporary name
// exists, and by a driver that re-owns the working set while it walks the tree —
// so no name moves under a walk that counts a file's names.
//
// It is waited for rather than refused, and the wait ends with the waiter's
// context: a waiter whose context is done leaves without the lock, having done
// nothing. Holding it is never given up early — it is released only by the
// holder, once its write or its walk has returned.
type WorkingSetLock struct {
	// held carries one token while the lock is taken.
	held chan struct{}
}

// NewWorkingSetLock returns an unheld lock.
func NewWorkingSetLock() *WorkingSetLock {
	return &WorkingSetLock{held: make(chan struct{}, 1)}
}

// Lock takes the lock, waiting for it until ctx is done. It returns ctx's error
// without the lock when ctx ended first — also when the lock came free in the
// same instant, so a caller that gets nil has a live context.
func (l *WorkingSetLock) Lock(ctx context.Context) error {
	select {
	case l.held <- struct{}{}:
	case <-ctx.Done():
		return ctx.Err()
	}
	if err := ctx.Err(); err != nil {
		l.Unlock()
		return err
	}
	return nil
}

// TryLock takes the lock if nobody holds it and reports whether it did.
func (l *WorkingSetLock) TryLock() bool {
	select {
	case l.held <- struct{}{}:
		return true
	default:
		return false
	}
}

// Unlock releases the lock. Only its holder may call it.
func (l *WorkingSetLock) Unlock() { <-l.held }
