//go:build linux

package hostprocess

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

// clockTicksPerSecond is the kernel USER_HZ used to convert /proc CPU ticks to
// seconds. It is fixed at 100 on every supported Linux kernel; reading it via
// sysconf(_SC_CLK_TCK) would need cgo, which the no-deps posture avoids
// (docs/dev/DEPENDENCIES.md).
const clockTicksPerSecond = 100

// readProcStats reads the process's resident set size (bytes) and total consumed
// CPU ticks (utime+stime) from /proc, plus the clock-ticks-per-second used to
// turn ticks into seconds. It errors when /proc is unreadable (e.g. the process
// has exited).
func readProcStats(pid int) (rssBytes uint64, cpuTicks uint64, hz uint64, err error) {
	statm, err := os.ReadFile(fmt.Sprintf("/proc/%d/statm", pid))
	if err != nil {
		return 0, 0, 0, fmt.Errorf("read statm: %w", err)
	}
	fields := strings.Fields(string(statm))
	if len(fields) < 2 {
		return 0, 0, 0, fmt.Errorf("statm: unexpected format %q", string(statm))
	}
	residentPages, err := strconv.ParseUint(fields[1], 10, 64)
	if err != nil {
		return 0, 0, 0, fmt.Errorf("statm resident: %w", err)
	}
	rssBytes = residentPages * uint64(os.Getpagesize())

	cpuTicks, err = readCPUTicks(pid)
	if err != nil {
		return 0, 0, 0, err
	}
	return rssBytes, cpuTicks, clockTicksPerSecond, nil
}

// readCPUTicks reads utime+stime (fields 14 and 15, 1-based) from /proc/<pid>/stat.
// The comm field (field 2) may contain spaces and parentheses, so parsing starts
// after the final ')'.
func readCPUTicks(pid int) (uint64, error) {
	stat, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return 0, fmt.Errorf("read stat: %w", err)
	}
	s := string(stat)
	end := strings.LastIndexByte(s, ')')
	if end < 0 || end+2 > len(s) {
		return 0, fmt.Errorf("stat: unexpected format")
	}
	// Fields after comm start at index 3 (state); utime is field 14, stime 15,
	// i.e. offsets 11 and 12 in this post-comm slice (0-based).
	fields := strings.Fields(s[end+2:])
	if len(fields) < 13 {
		return 0, fmt.Errorf("stat: too few fields")
	}
	utime, err := strconv.ParseUint(fields[11], 10, 64)
	if err != nil {
		return 0, fmt.Errorf("stat utime: %w", err)
	}
	stime, err := strconv.ParseUint(fields[12], 10, 64)
	if err != nil {
		return 0, fmt.Errorf("stat stime: %w", err)
	}
	return utime + stime, nil
}
