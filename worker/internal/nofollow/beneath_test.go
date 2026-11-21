package nofollow

import (
	"errors"
	"testing"

	"golang.org/x/sys/unix"
)

// Openat2Beneath retries an interrupted openat2, and reports openat2 as
// unavailable with ENOSYS however it learns so: from a kernel before 5.6
// (ENOSYS), or from a seccomp profile that predates openat2 and denies it
// (EPERM). Its callers fall back on that one errno, so neither must fail every
// call. Any other outcome passes through as it is.
func TestOpenat2BeneathReportsAnUnavailableOpenat2AsENOSYS(t *testing.T) {
	const openedFd = 7
	for _, tc := range []struct {
		results []error
		want    error
		calls   int
	}{
		{[]error{unix.ENOSYS}, unix.ENOSYS, 1},
		{[]error{unix.EPERM}, unix.ENOSYS, 1},
		{[]error{unix.EINTR, unix.EPERM}, unix.ENOSYS, 2},
		{[]error{unix.EINTR, unix.EINTR, nil}, nil, 3},
		{[]error{unix.ELOOP}, unix.ELOOP, 1},
		{[]error{unix.EXDEV}, unix.EXDEV, 1},
	} {
		orig := openat2
		calls := 0
		openat2 = func(int, string) (int, error) {
			calls++
			if err := tc.results[calls-1]; err != nil {
				return -1, err
			}
			return openedFd, nil
		}
		fd, err := Openat2Beneath(unix.AT_FDCWD, "dir")
		openat2 = orig
		if !errors.Is(err, tc.want) || calls != tc.calls || (err == nil && fd != openedFd) {
			t.Errorf("openat2 answering %v: Openat2Beneath = %d, %v after %d calls; want %v after %d",
				tc.results, fd, err, calls, tc.want, tc.calls)
		}
	}
}
