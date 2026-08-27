// Package javaproperties parses a Java ".properties" file the way
// java.util.Properties.load(InputStream) does, so the Worker reads a
// server.properties exactly as the Minecraft server it supervises will (issue
// #2811).
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
//   - Bytes decode as latin-1 (ISO-8859-1), which is what Properties.load does
//     with an InputStream; every byte maps to the code point of the same value.
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

import "strings"

// Parse parses the contents of a Java .properties file into its key/value pairs,
// last occurrence winning. It never fails: a .properties file has no syntax a
// reader can reject, and the one construct the reference implementation throws
// on -- a malformed \uXXXX escape -- is decoded here as the literal characters
// instead (see loadConvert). Callers own the I/O and its error policy; whole
// contents are parsed at once, so no line length truncates the parse.
func Parse(data []byte) map[string]string {
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

// loadConvert decodes raw as latin-1 and resolves the .properties escapes,
// mirroring Properties.loadConvert. A malformed \uXXXX -- which the reference
// implementation rejects with an exception -- yields the literal 'u' followed by
// whatever came after it, so a hand-mangled file is read rather than turning
// every caller into an error path (the Minecraft server refuses such a file
// outright, so no value we could return would match it anyway).
func loadConvert(raw []byte) string {
	var b strings.Builder
	b.Grow(len(raw))
	for i := 0; i < len(raw); i++ {
		c := raw[i]
		if c != '\\' || i+1 >= len(raw) {
			b.WriteRune(rune(c))
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
			b.WriteRune(rune(esc))
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
