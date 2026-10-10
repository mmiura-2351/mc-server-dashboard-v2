package javaproperties

import (
	"bufio"
	"strings"
	"testing"
)

// Keep parityCases aligned with the API's PARITY_CASES for latin-1 grammar.
// UTF-8 decoding and unpaired surrogates have separate tests because their representations differ.
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
		name:  "a continuation of a non-empty line is never a comment",
		input: "rcon.password=one\\\n#two\n",
		want:  map[string]string{"rcon.password": "one#two"},
	},
	{
		name:  "a lone backslash continuing a non-empty line joins the next line",
		input: "motd=a\\\n  \\\n!b\n",
		want:  map[string]string{"motd": "a!b"},
	},
	{
		name:  "a blank continuation line ends the value",
		input: "rcon.password=one\\\n\nmotd=hi\n",
		want:  map[string]string{"rcon.password": "one", "motd": "hi"},
	},
	// A zero-length continuation: a lone backslash, blanks aside, continues a logical line that is still empty, and
	// Java reads the line after it as the start of a logical line.
	{
		name:  "a zero-length continuation onto a blank line",
		input: "\\\n\n",
		want:  map[string]string{},
	},
	{
		name:  "a zero-length continuation onto a hash comment",
		input: "\\\n#comment\\\n",
		want:  map[string]string{},
	},
	{
		name:  "a zero-length continuation onto a bang comment",
		input: "\\\n!bang\n",
		want:  map[string]string{},
	},
	{
		name:  "a zero-length continuation onto a blank-led comment",
		input: "\\\n\t#c\nk=v\n",
		want:  map[string]string{"k": "v"},
	},
	{
		name:  "a zero-length continuation onto a property",
		input: "\\\n#c\\\nresource-pack=old\n",
		want:  map[string]string{"resource-pack": "old"},
	},
	{
		name:  "a zero-length continuation is an empty key at EOF",
		input: "\\\n",
		want:  map[string]string{"": ""},
	},
	{
		name:  "a zero-length continuation before a lone CR at EOF",
		input: "\\\r",
		want:  map[string]string{"": ""},
	},
	{
		name:  "a zero-length continuation before CRLF at EOF is nothing",
		input: "\\\r\n",
		want:  map[string]string{},
	},
	{
		name:  "two zero-length continuations ending in CRLF",
		input: "\\\n\\\r\n",
		want:  map[string]string{},
	},
	{
		name:  "a lone backslash after a hash comment is an empty key",
		input: "\\\n#c\\\n\\\n",
		want:  map[string]string{"": ""},
	},
	{
		name:  "a lone backslash after a bang comment is an empty key",
		input: "\\\n!c\\\n\\\n",
		want:  map[string]string{"": ""},
	},
	{
		name:  "a lone backslash after two comments is an empty key",
		input: "\\\n#a\\\n!b\\\n\\\n",
		want:  map[string]string{"": ""},
	},
	{
		name:  "a lone backslash after a comment ending in CRLF is nothing",
		input: "\\\n#c\\\n\\\r\n",
		want:  map[string]string{},
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
		name:  "an escaped hash in the key",
		input: `a\#b=tok` + "\n",
		want:  map[string]string{"a#b": "tok"},
	},
	{
		name:  "an escaped space in the key",
		input: `a\ b=tok` + "\n",
		want:  map[string]string{"a b": "tok"},
	},
	{
		name:  "an escaped bang in the key",
		input: `a\!b=tok` + "\n",
		want:  map[string]string{"a!b": "tok"},
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
		name:  "a surrogate pair escape is one character",
		input: `motd=\uD83D\uDE00` + "\n",
		want:  map[string]string{"motd": "\U0001F600"},
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
				if v, ok := got[k]; !ok || v != want {
					t.Errorf("Parse(%q) = %#v, want %#v", tc.input, got, tc.want)
				}
			}
		})
	}
}

// Run the shared grammar cases through ParseUTF8; ASCII is identical and invalid UTF-8 falls back to latin-1.
func TestParseUTF8KeepsTheGrammar(t *testing.T) {
	for _, tc := range parityCases {
		t.Run(tc.name, func(t *testing.T) {
			got := ParseUTF8([]byte(tc.input))
			if len(got) != len(tc.want) {
				t.Fatalf("ParseUTF8(%q) = %#v, want %#v", tc.input, got, tc.want)
			}
			for k, want := range tc.want {
				if v, ok := got[k]; !ok || v != want {
					t.Errorf("ParseUTF8(%q) = %#v, want %#v", tc.input, got, tc.want)
				}
			}
		})
	}
}

// TestParseUTF8DecodesAsMinecraft120Does pins the reader a Minecraft 1.20+ server loads server.properties with
// (Settings.loadFromFile): UTF-8, and latin-1 for the WHOLE file when any byte of it is not valid UTF-8.
func TestParseUTF8DecodesAsMinecraft120Does(t *testing.T) {
	for _, tc := range []struct {
		name  string
		input string
		want  map[string]string
	}{
		{
			name:  "a UTF-8 value decodes as UTF-8",
			input: "rcon.password=caf\xc3\xa9\n",
			want:  map[string]string{"rcon.password": "café"},
		},
		{
			name:  "a file that is not valid UTF-8 decodes as latin-1",
			input: "rcon.password=caf\xe9\n",
			want:  map[string]string{"rcon.password": "café"},
		},
		{
			name:  "one invalid byte turns the whole file latin-1",
			input: "motd=caf\xc3\xa9\nrcon.password=caf\xe9\n",
			want:  map[string]string{"motd": "cafÃ©", "rcon.password": "café"},
		},
		{
			name:  "an escaped UTF-8 character stands for itself",
			input: "rcon.password=caf\\\xc3\xa9\n",
			want:  map[string]string{"rcon.password": "café"},
		},
		{
			name:  "a unicode escape decodes as before",
			input: `rcon.password=caf\u00e9` + "\n",
			want:  map[string]string{"rcon.password": "café"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := ParseUTF8([]byte(tc.input))
			if len(got) != len(tc.want) {
				t.Fatalf("ParseUTF8(%q) = %#v, want %#v", tc.input, got, tc.want)
			}
			for k, want := range tc.want {
				if got[k] != want {
					t.Errorf("ParseUTF8(%q)[%q] = %q, want %q", tc.input, k, got[k], want)
				}
			}
		})
	}
}

// Pair valid UTF-16 surrogate escapes; replace unpaired units with U+FFFD because they have no UTF-8 encoding.
func TestParseSurrogateEscapes(t *testing.T) {
	for _, tc := range []struct {
		name  string
		input string
		want  string
	}{
		{"a pair is one character", `motd=\uD83D\uDE00`, "\U0001F600"},
		{"text after a pair is kept", `motd=\uD83D\uDE00x`, "\U0001F600x"},
		{"text before a pair is kept", `motd=x\uD83D\uDE00`, "x\U0001F600"},
		{"two pairs in a row", `motd=\uD83D\uDE00\uD83D\uDE01`, "\U0001F600\U0001F601"},
		{"a high half followed by a BMP escape is not a pair", `motd=\uD83D\u0041`, "\uFFFDA"},
		{"a high half followed by another high half is not a pair", `motd=\uD83D\uD83D\uDE00`, "\uFFFD\U0001F600"},
		{"a high half followed by literal text is not a pair", `motd=\uD83Dx`, "\uFFFDx"},
		{"a high half at the end of the value is not a pair", `motd=\uD83D`, "\uFFFD"},
		{"a lone low half is not a pair", `motd=\uDE00`, "\uFFFD"},
		{"a low half followed by a high half is not a pair", `motd=\uDE00\uD83D`, "\uFFFD\uFFFD"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := Parse([]byte(tc.input + "\n"))
			if got["motd"] != tc.want {
				t.Errorf("Parse(%q)[\"motd\"] = %q, want %q", tc.input, got["motd"], tc.want)
			}
		})
	}
}

// TestParseHandlesLinesLongerThanTheScannerCap pins that the parser has no bufio.Scanner token cap: a huge motd
// used to truncate the parse and drop every key after it, which the container driver could only defend against
// by failing the start outright.
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
