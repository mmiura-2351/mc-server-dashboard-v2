package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/config"
)

// escapedRunes are the characters a log line never carries raw (issue #3939),
// the set the API escapes too (CONFIGURATION.md Section 5.10): DEL, C1, the line
// and paragraph separators and the bidi controls, by their boundaries. slog
// escapes C0 itself, but writes DEL in either format, and C1 and the bidi
// controls in json, as they are.
var escapedRunes = []rune{
	0x7f, 0x80, 0x85, 0x9b, 0x9f, 0x061c, 0x200e, 0x200f,
	0x2028, 0x2029, 0x202a, 0x202e, 0x2066, 0x2069,
}

// keptRunes are their neighbours, which json writes as they are.
var keptRunes = []rune{
	0x7e, 0xa0, 0x061b, 0x061d, 0x200d, 0x2010, 0x2027, 0x202f, 0x2065, 0x206a,
}

// logLine logs one record through newLogger carrying s in its message, an
// attribute key, an attribute value and an error, and returns its line.
// newLogger writes to os.Stderr, so a pipe stands in for it while the logger is
// built; one record fits the pipe's buffer.
func logLine(t *testing.T, format, s string) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = r.Close() }()
	orig := os.Stderr
	os.Stderr = w
	logger := newLogger(config.LogConfig{Format: format})
	os.Stderr = orig
	logger.Info("a"+s, "k"+s, "v"+s, "err", errors.New("e"+s))
	_ = w.Close()
	b, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	out := string(b)
	line, ok := strings.CutSuffix(out, "\n")
	if !ok || strings.Contains(line, "\n") {
		t.Fatalf("want exactly one line, got %q", out)
	}
	return line
}

// assertJSONRecord checks that a json line still decodes to what was logged.
func assertJSONRecord(t *testing.T, line, s string) {
	t.Helper()
	var rec map[string]any
	if err := json.Unmarshal([]byte(line), &rec); err != nil {
		t.Fatalf("not json: %v: %q", err, line)
	}
	if rec["msg"] != "a"+s || rec["k"+s] != "v"+s || rec["err"] != "e"+s {
		t.Errorf("decoded %q, want msg=%q %q=%q err=%q", rec, "a"+s, "k"+s, "v"+s, "e"+s)
	}
}

func TestNewLoggerEscapesControlCharacters(t *testing.T) {
	for _, format := range []string{"json", "text"} {
		for _, r := range escapedRunes {
			t.Run(fmt.Sprintf("%s/%U", format, r), func(t *testing.T) {
				line := logLine(t, format, string(r))
				if strings.ContainsRune(line, r) {
					t.Errorf("%U written raw: %q", r, line)
				}
				if format == "json" {
					assertJSONRecord(t, line, string(r))
				}
			})
		}
	}
}

func TestNewLoggerKeepsOtherCharacters(t *testing.T) {
	for _, r := range keptRunes {
		t.Run(fmt.Sprintf("%U", r), func(t *testing.T) {
			line := logLine(t, "json", string(r))
			if got := strings.Count(line, string(r)); got != 4 {
				t.Errorf("%U written as is %d times, want 4: %q", r, got, line)
			}
			assertJSONRecord(t, line, string(r))
		})
	}
}
