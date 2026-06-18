package hostresources

import (
	"runtime"
	"strings"
	"testing"
)

func TestCPUCores(t *testing.T) {
	got := CPUCores()
	want := uint32(runtime.NumCPU())
	if got != want {
		t.Errorf("CPUCores() = %d, want %d", got, want)
	}
	if got == 0 {
		t.Error("CPUCores() = 0, want > 0")
	}
}

func TestParseMemTotal(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  uint64
	}{
		{
			name: "typical /proc/meminfo",
			input: `MemTotal:       16384000 kB
MemFree:          123456 kB
MemAvailable:    8000000 kB
`,
			want: 16384000 * 1024,
		},
		{
			name: "single line",
			input: `MemTotal:       8192000 kB
`,
			want: 8192000 * 1024,
		},
		{
			name:  "missing MemTotal",
			input: "MemFree: 123456 kB\n",
			want:  0,
		},
		{
			name:  "empty input",
			input: "",
			want:  0,
		},
		{
			name:  "malformed value",
			input: "MemTotal:       notanumber kB\n",
			want:  0,
		},
		{
			name:  "missing unit",
			input: "MemTotal:       16384000\n",
			want:  0,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := parseMemTotal(strings.NewReader(tt.input))
			if got != tt.want {
				t.Errorf("parseMemTotal() = %d, want %d", got, tt.want)
			}
		})
	}
}

func TestMemoryBytesReturnsPositive(t *testing.T) {
	// On Linux (CI / dev), /proc/meminfo exists and the result must be positive.
	// On non-Linux, the function returns 0 gracefully — the test only asserts
	// that it does not panic.
	got := MemoryBytes()
	if runtime.GOOS == "linux" && got == 0 {
		t.Error("MemoryBytes() = 0 on Linux, want > 0")
	}
}
