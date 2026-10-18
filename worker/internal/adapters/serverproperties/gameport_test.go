package serverproperties

import (
	"strings"
	"testing"
)

// GamePort resolves server-port as the Minecraft server does, exactly as RCONPort
// resolves rcon.port: Integer.parseInt, the default when that throws, and an
// out-of-range integer refused without the value in the error (issue #3417).
func TestGamePort(t *testing.T) {
	for _, tc := range []struct {
		value string
		want  int
	}{
		{"", DefaultGamePort},
		{"26590", 26590},
		{"+26590", 26590},
		{"1", 1},
		{"65535", 65535},
		{"notaport", DefaultGamePort},
		{"25565rcon.password=SECRET", DefaultGamePort},
		{"25565 ", DefaultGamePort},
		{"2147483648", DefaultGamePort}, // overflows int32: parseInt throws
	} {
		got, err := GamePort(tc.value)
		if err != nil || got != tc.want {
			t.Errorf("GamePort(%q) = %d, %v; want %d, nil", tc.value, got, err, tc.want)
		}
	}
	for _, value := range []string{"0", "-1", "65536", "70000"} {
		_, err := GamePort(value)
		if err == nil {
			t.Errorf("GamePort(%q) = nil error, want it refused as out of range", value)
			continue
		}
		if !strings.Contains(err.Error(), "server-port") || strings.Contains(err.Error(), value) {
			t.Errorf("GamePort(%q) error = %q, want it to name server-port and not repeat the value", value, err)
		}
	}
}
