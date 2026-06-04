//go:build !linux

package hostprocess

import "errors"

// readProcStats is unsupported off Linux: /proc-based metrics are Linux-specific.
// The instance manager treats the error as "no measurement" and emits an up-only
// metrics sample (FR-MON-3).
func readProcStats(int) (rssBytes uint64, cpuTicks uint64, hz uint64, err error) {
	return 0, 0, 0, errors.New("hostprocess: /proc metrics unsupported on this platform")
}
