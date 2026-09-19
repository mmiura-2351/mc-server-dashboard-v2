// Package javaproperties parses a Java ".properties" file with the grammar of
// java.util.Properties.load, so the Worker reads a server.properties as the
// Minecraft server it supervises does (issue #2811). Which charset the server
// decodes the file in depends on its version: latin-1 before Minecraft 1.20
// (Parse), UTF-8 first with a latin-1 fallback from 1.20 (ParseUTF8, issue
// #3116). The two readings differ only in how a non-ASCII byte decodes, never in
// the ASCII structure the grammar walks (a UTF-8 multi-byte sequence holds no
// ASCII byte), so they agree on every pure-ASCII key and value.
//
// The three adapters that need values out of a server.properties -- the RCON
// credentials, the container driver's published ports, and the tunnel's game
// port -- used to carry a "key=value"-only copy of a parser each. Java accepts
// considerably more than that, so a respelled line ("rcon.password:evil",
// "server-port 25599", an escaped or \uXXXX-spelled key, a backslash
// continuation) read one way here and another way in the server. This package is
// the single reader all three share.
//
// The grammar, following the reference implementation:
//
//   - Parse decodes bytes as latin-1 (ISO-8859-1), which is what
//     Properties.load does with an InputStream: every byte maps to the code
//     point of the same value. ParseUTF8 decodes them as UTF-8, or as latin-1
//     when the file is not valid UTF-8. Every rule below holds for both.
//   - A line ends at "\n", "\r\n" or a lone "\r". Leading whitespace (space, tab,
//     form feed) is skipped, and a line that is then empty is ignored.
//   - A line whose first non-whitespace character is '#' or '!' is a comment and
//     is dropped -- a comment does NOT continue on a trailing backslash.
//   - A line ending in an ODD number of backslashes continues onto the next
//     line, whose own leading whitespace is skipped; a continuation line is
//     never a comment, and a blank continuation line ends the value.
//   - The key runs to the first unescaped '=', ':' or whitespace. Whitespace
//     after the key, then one optional '=' or ':', then further whitespace are
//     skipped; everything remaining -- trailing whitespace included -- is the
//     value.
//   - "\t", "\r", "\n", "\f" and "\uXXXX" are decoded in both key and value; any
//     other escaped character stands for itself.
//   - A key repeated in the file takes its LAST occurrence's value.
package javaproperties

import (
	"strings"
	"unicode/utf8"
)

// Parse parses the contents of a Java .properties file into its key/value pairs,
// last occurrence winning, decoding it as latin-1 -- how a Minecraft server
// before 1.20 reads server.properties. It never fails: a .properties file has no syntax a
// reader can reject, and the one construct the reference implementation throws
// on -- a malformed \uXXXX escape -- is decoded here as the literal characters
// instead (see loadConvert). Callers own the I/O and its error policy; whole
// contents are parsed at once, so no line length truncates the parse.
func Parse(data []byte) map[string]string {
	return parse(latin1ToUTF8(data))
}

// ParseUTF8 is Parse with the charset a Minecraft 1.20+ server reads its
// server.properties in: UTF-8, or latin-1 for the WHOLE file when data is not
// valid UTF-8. Its decoder reports the first malformed byte, and the server then
// reloads the file from the start as ISO-8859-1 (Settings.loadFromFile).
func ParseUTF8(data []byte) map[string]string {
	if !utf8.Valid(data) {
		return Parse(data)
	}
	return parse(data)
}

// parse runs the grammar over text, which is UTF-8. Every byte the grammar acts
// on is ASCII and no byte of a UTF-8 multi-byte sequence is, so this byte-wise
// walk splits text exactly where Java's char-wise one splits the decoded file.
func parse(data []byte) map[string]string {
	out := map[string]string{}
	for i := 0; i < len(data); {
		line, next := naturalLine(data, i)
		line = trimLeadingBlanks(line)
		if len(line) == 0 || line[0] == '#' || line[0] == '!' {
			i = next
			continue
		}
		logical := line
		i = next
		for endsWithOddBackslash(logical) {
			logical = logical[:len(logical)-1]
			if i >= len(data) {
				break
			}
			line, next = naturalLine(data, i)
			logical = append(append([]byte{}, logical...), trimLeadingBlanks(line)...)
			i = next
		}
		key, value := splitKeyValue(logical)
		out[key] = value
	}
	return out
}

// latin1ToUTF8 decodes data as latin-1 (ISO-8859-1) into UTF-8 text: every byte
// becomes the code point of the same value.
func latin1ToUTF8(data []byte) []byte {
	out := make([]byte, 0, len(data))
	for _, c := range data {
		out = utf8.AppendRune(out, rune(c))
	}
	return out
}

// naturalLine returns the bytes of the line starting at off, without its
// terminator, and the offset of the next line. "\r\n", a lone "\r" and a lone
// "\n" all terminate; an unterminated final line runs to the end.
func naturalLine(data []byte, off int) (line []byte, next int) {
	end := off
	for end < len(data) && data[end] != '\n' && data[end] != '\r' {
		end++
	}
	if end >= len(data) {
		return data[off:end], end
	}
	if data[end] == '\r' && end+1 < len(data) && data[end+1] == '\n' {
		return data[off:end], end + 2
	}
	return data[off:end], end + 1
}

// trimLeadingBlanks drops the leading space / tab / form feed run.
func trimLeadingBlanks(line []byte) []byte {
	i := 0
	for i < len(line) && isBlank(line[i]) {
		i++
	}
	return line[i:]
}

func isBlank(c byte) bool { return c == ' ' || c == '\t' || c == '\f' }

// endsWithOddBackslash reports whether line ends in an odd-length backslash run,
// which is what makes it continue onto the next line (an even run is a sequence
// of escaped backslashes and terminates the logical line).
func endsWithOddBackslash(line []byte) bool {
	n := 0
	for i := len(line) - 1; i >= 0 && line[i] == '\\'; i-- {
		n++
	}
	return n%2 == 1
}

// splitKeyValue splits one logical line (already stripped of its leading
// whitespace) into its decoded key and value.
func splitKeyValue(line []byte) (key, value string) {
	keyEnd := len(line)
	valueStart := len(line)
	hasSep := false
	backslash := false
	for i := 0; i < len(line); i++ {
		c := line[i]
		if !backslash && (c == '=' || c == ':') {
			keyEnd, valueStart, hasSep = i, i+1, true
			break
		}
		if !backslash && isBlank(c) {
			keyEnd, valueStart = i, i+1
			break
		}
		backslash = c == '\\' && !backslash
	}
	for valueStart < len(line) {
		c := line[valueStart]
		if !isBlank(c) {
			if hasSep || (c != '=' && c != ':') {
				break
			}
			hasSep = true
		}
		valueStart++
	}
	return loadConvert(line[:keyEnd]), loadConvert(line[valueStart:])
}

// loadConvert resolves the .properties escapes in raw, which is UTF-8 text,
// mirroring Properties.loadConvert. A malformed \uXXXX -- which the reference
// implementation rejects with an exception -- yields the literal 'u' followed by
// whatever came after it, so a hand-mangled file is read rather than turning
// every caller into an error path (the Minecraft server refuses such a file
// outright, so no value we could return would match it anyway).
//
// A \uD800-\uDFFF escape spells half a surrogate pair, which a Go string cannot
// hold, so it becomes U+FFFD here where Java keeps the lone surrogate. No key
// the Worker reads can be spelled that way and the result is still stable, so
// this is the one place the parse is not byte-identical to Java's.
func loadConvert(raw []byte) string {
	var b strings.Builder
	b.Grow(len(raw))
	for i := 0; i < len(raw); i++ {
		c := raw[i]
		if c != '\\' || i+1 >= len(raw) {
			b.WriteByte(c)
			continue
		}
		i++
		switch esc := raw[i]; esc {
		case 'u':
			if v, ok := hex4(raw, i+1); ok {
				b.WriteRune(v)
				i += 4
			} else {
				b.WriteByte('u')
			}
		case 't':
			b.WriteByte('\t')
		case 'r':
			b.WriteByte('\r')
		case 'n':
			b.WriteByte('\n')
		case 'f':
			b.WriteByte('\f')
		default:
			// Only the escape's first byte is consumed here; an escaped
			// multi-byte character's remaining bytes follow as ordinary text.
			b.WriteByte(esc)
		}
	}
	return b.String()
}

// hex4 decodes the four hex digits at off into the code point they spell.
func hex4(raw []byte, off int) (rune, bool) {
	if off+4 > len(raw) {
		return 0, false
	}
	v := 0
	for _, c := range raw[off : off+4] {
		switch {
		case c >= '0' && c <= '9':
			v = v<<4 + int(c-'0')
		case c >= 'a' && c <= 'f':
			v = v<<4 + int(c-'a') + 10
		case c >= 'A' && c <= 'F':
			v = v<<4 + int(c-'A') + 10
		default:
			return 0, false
		}
	}
	return rune(v), true
}
