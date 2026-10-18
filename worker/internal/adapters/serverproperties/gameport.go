package serverproperties

// DefaultGamePort is the port a Minecraft server's game listener binds when
// server.properties gives no usable server-port.
const DefaultGamePort = 25565

// GamePort resolves a server-port value to the port the Minecraft server listens
// on, for both the game-port publication (containerdriver) and the relay tunnel's
// dial (tunnel), so the two cannot disagree. The server reads server-port as it
// reads rcon.port, so this is RCONPort with DefaultGamePort as the fallback: a
// value that is not a 32-bit decimal integer -- absent, or a backslash-continued
// line that folded the next line (a password, say) into it -- is
// DefaultGamePort, and an integer outside 1-65535 is an error that does not
// repeat the value (issue #3417). Callers build what they dial or publish from
// the returned number, never from the text.
func GamePort(value string) (int, error) {
	return port("server-port", value, DefaultGamePort)
}
