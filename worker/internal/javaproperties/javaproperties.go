// Package javaproperties implements Java properties grammar for RCON and game-port readers.
// Parse decodes latin-1; ParseUTF8 uses UTF-8 with whole-file latin-1 fallback.
//
// Keys end at unescaped equals, colon, or whitespace; duplicate keys use the last value.
// Leading space, tab, and form feed are ignored; trailing value whitespace is retained.
//
// LF, CRLF, and lone CR terminate lines. Odd trailing backslashes continue a value.
// A comment does not continue; an empty continuation still permits a new comment or blank line.
//
// Standard escapes and Unicode escapes are decoded; malformed Unicode stays literal.
// Valid UTF-16 surrogate pairs become one rune, while unpaired units become U+FFFD.
package javaproperties

import (
	"bytes"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// Parse decodes latin-1 with Java properties grammar; duplicate keys use the last value.
// Malformed Unicode escapes remain literal instead of raising Java's exception.
func Parse(data []byte) map[string]string {
	return parse(latin1ToUTF8(data))
}

// ParseUTF8 uses UTF-8 when valid; otherwise the entire file falls back to latin-1, matching Minecraft 1.20+.
func ParseUTF8(data []byte) map[string]string {
	if !utf8.Valid(data) {
		return Parse(data)
	}
	return parse(data)
}

// Grammar bytes are ASCII, which cannot occur within UTF-8 multibyte sequences, so byte-wise splitting is safe.
func parse(data []byte) map[string]string {
	out := map[string]string{}
	for i := 0; i < len(data); {
		line, next := naturalLine(data, i)
		line = trimLeadingBlanks(line)
		i = next
		for isLoneBackslash(line) && i < len(data) {
			line, i = naturalLine(data, i)
			line = trimLeadingBlanks(line)
		}
		if isLoneBackslash(line) && bytes.HasSuffix(data, []byte("\r\n")) {
			continue
		}
		if len(line) == 0 || line[0] == '#' || line[0] == '!' {
			continue
		}
		logical := line
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

func latin1ToUTF8(data []byte) []byte {
	out := make([]byte, 0, len(data))
	for _, c := range data {
		out = utf8.AppendRune(out, rune(c))
	}
	return out
}

// naturalLine accepts LF, CRLF, and lone CR, excluding the terminator.
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

func trimLeadingBlanks(line []byte) []byte {
	i := 0
	for i < len(line) && isBlank(line[i]) {
		i++
	}
	return line[i:]
}

func isBlank(c byte) bool { return c == ' ' || c == '\t' || c == '\f' }

// A lone backslash continues without contributing text, so the next line is still a logical-line start.
func isLoneBackslash(line []byte) bool { return len(line) == 1 && line[0] == '\\' }

// An odd trailing backslash continues; an even run ends the logical line.
func endsWithOddBackslash(line []byte) bool {
	n := 0
	for i := len(line) - 1; i >= 0 && line[i] == '\\'; i-- {
		n++
	}
	return n%2 == 1
}

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

// loadConvert keeps malformed Unicode escapes literal and combines valid UTF-16 surrogate pairs.
// Unpaired surrogates become U+FFFD because Go cannot encode Java's lone UTF-16 units.
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
			if v, n, ok := unicodeEscape(raw, i+1); ok {
				b.WriteRune(v)
				i += n
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

// unicodeEscape consumes a following low surrogate with a high surrogate to produce one rune.
// Unpaired surrogates are left for WriteRune to replace with U+FFFD.
func unicodeEscape(raw []byte, off int) (r rune, consumed int, ok bool) {
	v, ok := hex4(raw, off)
	if !ok {
		return 0, 0, false
	}
	if utf16.IsSurrogate(v) && off+6 <= len(raw) && raw[off+4] == '\\' && raw[off+5] == 'u' {
		if lo, ok := hex4(raw, off+6); ok {
			if paired := utf16.DecodeRune(v, lo); paired != utf8.RuneError {
				return paired, 10, true
			}
		}
	}
	return v, 4, true
}

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
