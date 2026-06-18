// Package hostresources reads the host's CPU and memory information using Go
// stdlib and /proc/meminfo, avoiding external dependencies (issue #1218).
package hostresources

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"runtime"
	"strconv"
	"strings"
)

// memInfoPath is the path to the Linux meminfo file. It is a variable so tests
// can replace it with a fixture.
var memInfoPath = "/proc/meminfo"

// CPUCores returns the number of logical CPUs visible to the process.
func CPUCores() uint32 {
	return uint32(runtime.NumCPU())
}

// MemoryBytes returns the total physical memory in bytes, read from
// /proc/meminfo. It returns 0 on any error (non-Linux, missing file, parse
// failure) so a best-effort caller can proceed with placement disabled.
func MemoryBytes() uint64 {
	f, err := os.Open(memInfoPath)
	if err != nil {
		return 0
	}
	defer func() { _ = f.Close() }()
	return parseMemTotal(f)
}

// parseMemTotal extracts MemTotal from a /proc/meminfo-formatted reader. The
// line format is "MemTotal:   <value> kB". Returns 0 if not found or
// unparseable.
func parseMemTotal(r io.Reader) uint64 {
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "MemTotal:") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 2 {
			return 0
		}
		kb, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil {
			return 0
		}
		// /proc/meminfo reports kB (1024 bytes per unit).
		if len(fields) >= 3 && fields[2] == "kB" {
			return kb * 1024
		}
		return 0
	}
	return 0
}

// String returns a human-readable summary suitable for startup logs.
func String(cpuCores uint32, memoryBytes uint64) string {
	return fmt.Sprintf("cpu_cores=%d memory_bytes=%d", cpuCores, memoryBytes)
}
