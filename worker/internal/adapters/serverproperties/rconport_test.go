package serverproperties

import (
	"strings"
	"testing"
)

// RCONPort resolves rcon.port as the Minecraft server does: Integer.parseInt,
// the default when that throws, and an out-of-range integer refused without the
// value in the error (issue #3345).
func TestRCONPort(t *testing.T) {
	for _, tc := range []struct {
		value string
		want  int
	}{
		{"", DefaultRCONPort},
		{"26000", 26000},
		{"+26000", 26000},
		{"1", 1},
		{"65535", 65535},
		{"notaport", DefaultRCONPort},
		{"25575rcon.password=SECRET", DefaultRCONPort},
		{"25575 ", DefaultRCONPort},
		{"2147483648", DefaultRCONPort}, // overflows int32: parseInt throws
	} {
		got, err := RCONPort(tc.value)
		if err != nil || got != tc.want {
			t.Errorf("RCONPort(%q) = %d, %v; want %d, nil", tc.value, got, err, tc.want)
		}
	}
	for _, value := range []string{"0", "-1", "65536", "70000"} {
		_, err := RCONPort(value)
		if err == nil {
			t.Errorf("RCONPort(%q) = nil error, want it refused as out of range", value)
			continue
		}
		if strings.Contains(err.Error(), value) {
			t.Errorf("RCONPort(%q) error = %q, must not repeat the value", value, err)
		}
	}
}
