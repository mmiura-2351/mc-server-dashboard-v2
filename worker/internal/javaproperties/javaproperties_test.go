package javaproperties

import (
	"bufio"
	"strings"
	"testing"
)

// parityCases is the parse table mirrored one-for-one by the API's
// tests/servers/test_server_properties.py::PARITY_CASES. Same input, same
// parse -- that mirroring is the evidence that the platform-key guard and the
// worker read a server.properties alike (issue #2811). Keep the two tables in
// sync: a case added here but not there leaves the invariant unpinned.
var parityCases = []struct {
	name  string
	input string
	want  map[string]string
}{
	{
		name:  "equals separator",
		input: "server-port=25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "colon separator",
		input: "server-port:25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "whitespace separator",
		input: "server-port 25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "separator with surrounding whitespace",
		input: "server-port = 25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "whitespace then colon separator",
		input: "server-port : 25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "tab separator",
		input: "server-port\t25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "leading whitespace before the key",
		input: "   server-port=25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "a second separator belongs to the value",
		input: "server-port==25599\n",
		want:  map[string]string{"server-port": "=25599"},
	},
	{
		name:  "trailing whitespace is part of the value",
		input: "server-port=25599  \n",
		want:  map[string]string{"server-port": "25599  "},
	},
	{
		name:  "a key with no separator has an empty value",
		input: "server-port\n",
		want:  map[string]string{"server-port": ""},
	},
	{
		name:  "a hash comment is skipped",
		input: "#server-port=1\nserver-port=25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "a bang comment is skipped",
		input: "!server-port=1\nserver-port=25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "a comment does not continue on a trailing backslash",
		input: "#server-port=1\\\nserver-port=25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "blank lines are skipped",
		input: "\n   \nserver-port=25599\n",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "a backslash continues onto the next line",
		input: "rcon.password=one\\\n  two\n",
		want:  map[string]string{"rcon.password": "onetwo"},
	},
	{
		name:  "an even trailing backslash run does not continue",
		input: "rcon.password=one\\\\\nmotd=hi\n",
		want:  map[string]string{"rcon.password": `one\`, "motd": "hi"},
	},
	{
		name:  "a continuation line is never a comment",
		input: "rcon.password=one\\\n#two\n",
		want:  map[string]string{"rcon.password": "one#two"},
	},
	{
		name:  "a blank continuation line ends the value",
		input: "rcon.password=one\\\n\nmotd=hi\n",
		want:  map[string]string{"rcon.password": "one", "motd": "hi"},
	},
	{
		name:  "an escaped dot in the key",
		input: `rcon\.password=tok` + "\n",
		want:  map[string]string{"rcon.password": "tok"},
	},
	{
		name:  "an escaped separator in the key",
		input: `rcon\=password=tok` + "\n",
		want:  map[string]string{"rcon=password": "tok"},
	},
	{
		name:  "a unicode escape in the key",
		input: `\u0072con.password=tok` + "\n",
		want:  map[string]string{"rcon.password": "tok"},
	},
	{
		name:  "a unicode escape in the value",
		input: `motd=caf\u00e9` + "\n",
		want:  map[string]string{"motd": "café"},
	},
	{
		name:  "an escaped colon in the value",
		input: `motd=a\:b` + "\n",
		want:  map[string]string{"motd": "a:b"},
	},
	{
		name:  "an escaped leading hash in the value",
		input: `motd=\#hi` + "\n",
		want:  map[string]string{"motd": "#hi"},
	},
	{
		name:  "an escaped leading space in the value",
		input: `motd=\ hi` + "\n",
		want:  map[string]string{"motd": " hi"},
	},
	{
		name:  "control-character escapes in the value",
		input: `motd=a\tb\nc` + "\n",
		want:  map[string]string{"motd": "a\tb\nc"},
	},
	{
		name:  "a carriage-return escape in the value",
		input: `motd=a\rb` + "\n",
		want:  map[string]string{"motd": "a\rb"},
	},
	{
		name:  "a form-feed escape in the value",
		input: `motd=a\fb` + "\n",
		want:  map[string]string{"motd": "a\fb"},
	},
	{
		name:  "the last occurrence wins",
		input: "server-port=1\nserver-port:2\n",
		want:  map[string]string{"server-port": "2"},
	},
	{
		name:  "CRLF terminates a line",
		input: "server-port=25599\r\nmotd=hi\r\n",
		want:  map[string]string{"server-port": "25599", "motd": "hi"},
	},
	{
		name:  "a lone CR terminates a line",
		input: "server-port=25599\rmotd=hi\r",
		want:  map[string]string{"server-port": "25599", "motd": "hi"},
	},
	{
		name:  "a final line without a terminator is parsed",
		input: "server-port=25599",
		want:  map[string]string{"server-port": "25599"},
	},
	{
		name:  "a trailing lone backslash at EOF is dropped",
		input: `motd=hi\`,
		want:  map[string]string{"motd": "hi"},
	},
	{
		name:  "latin-1 bytes decode byte-for-byte",
		input: "motd=caf\xe9\n",
		want:  map[string]string{"motd": "café"},
	},
	{
		name:  "a malformed unicode escape keeps the u literal",
		input: `motd=a\uZZZZb` + "\n",
		want:  map[string]string{"motd": "auZZZZb"},
	},
	{
		name:  "an empty file has no properties",
		input: "",
		want:  map[string]string{},
	},
}

func TestParseParity(t *testing.T) {
	for _, tc := range parityCases {
		t.Run(tc.name, func(t *testing.T) {
			got := Parse([]byte(tc.input))
			if len(got) != len(tc.want) {
				t.Fatalf("Parse(%q) = %#v, want %#v", tc.input, got, tc.want)
			}
			for k, want := range tc.want {
				if got[k] != want {
					t.Errorf("Parse(%q)[%q] = %q, want %q", tc.input, k, got[k], want)
				}
			}
		})
	}
}

// TestParseHandlesLinesLongerThanTheScannerCap pins that the parser has no
// bufio.Scanner token cap: a huge motd used to truncate the parse and drop every
// key after it (issue #2811), which the container driver could only defend
// against by failing the start outright.
func TestParseHandlesLinesLongerThanTheScannerCap(t *testing.T) {
	huge := strings.Repeat("x", bufio.MaxScanTokenSize+1)
	props := Parse([]byte("motd=" + huge + "\nserver-port=26590\n"))

	if props["motd"] != huge {
		t.Errorf("motd length = %d, want %d", len(props["motd"]), len(huge))
	}
	if props["server-port"] != "26590" {
		t.Errorf("server-port = %q, want %q (a long line must not truncate the parse)", props["server-port"], "26590")
	}
}
