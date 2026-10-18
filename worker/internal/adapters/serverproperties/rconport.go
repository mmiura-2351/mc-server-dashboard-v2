package serverproperties

import (
	"errors"
	"strconv"
)

// DefaultRCONPort is the port a Minecraft server's RCON listens on when
// server.properties gives no usable rcon.port.
const DefaultRCONPort = 25575

// RCONPort resolves an rcon.port value to the port the Minecraft server listens
// on, for both the RCON dial (rcon) and the port publication (containerdriver),
// so the two cannot disagree. The server reads the value with Integer.parseInt
// and takes the default when that throws, so a value that is not a 32-bit
// decimal integer -- absent, or a backslash-continued line that folded the next
// line (a password, say) into it -- is DefaultRCONPort (issue #3345). Java also
// parses non-ASCII Unicode digits, which this does not; such a value takes the
// default here.
//
// An integer outside 1-65535 is an error that does not repeat the value, as no
// error built from server.properties text may: callers log and report it. Callers
// build what they dial or publish from the returned number, never from the text.
func RCONPort(value string) (int, error) {
	return port("rcon.port", value, DefaultRCONPort)
}

// port resolves a server.properties port value as RCONPort documents: fallback
// when the value is not a 32-bit decimal integer, an error naming key but not
// the value when it is an integer outside 1-65535.
func port(key, value string, fallback int) (int, error) {
	p, err := strconv.ParseInt(value, 10, 32)
	if err != nil {
		return fallback, nil
	}
	if p < 1 || p > 65535 {
		return 0, errors.New(key + " in server.properties is not a port number (1-65535)")
	}
	return int(p), nil
}
