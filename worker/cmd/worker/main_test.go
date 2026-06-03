package main

import (
	"bytes"
	"strings"
	"testing"
)

// Placeholder test that verifies the test runner is wired up by exercising the
// stub entry point's only observable behavior.
func TestRunPrintsBanner(t *testing.T) {
	var buf bytes.Buffer

	if err := run(&buf); err != nil {
		t.Fatalf("run() returned error: %v", err)
	}

	got := strings.TrimSpace(buf.String())
	if got != banner {
		t.Errorf("run() wrote %q, want %q", got, banner)
	}
}
