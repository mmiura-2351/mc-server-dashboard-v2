//go:build e2e

// Package e2e exercises the REAL Go data-plane client against a REAL running
// Python API (issue #111). Unlike the unit tests in the datatransfer package
// (which drive the client against an httptest fake), this harness proves the
// archive conventions — tar member forms, sanitization, status codes, the auth
// header — line up end to end across the two languages.
//
// It is gated two ways so it never runs in the ordinary `go test ./...` pass:
//   - the `e2e` build tag (this file compiles only under `-tags e2e`), and
//   - the MCD_E2E_API_URL + MCD_E2E_CREDENTIAL environment variables (the test
//     skips when either is unset).
//
// The CI job (.github/workflows/e2e.yml) boots the API, sets these, and runs
// `go test -tags e2e ./test/e2e/...`. See worker/README.md for a local dry run.
package e2e

import (
	"context"
	"crypto/rand"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/datatransfer"
)

// env reads a required environment variable or skips the whole test when unset,
// so the harness is inert outside the CI job (or a deliberate local run).
func env(t *testing.T, name string) string {
	t.Helper()
	v := os.Getenv(name)
	if v == "" {
		t.Skipf("%s not set; skipping cross-language e2e", name)
	}
	return v
}

// scopeURL builds a data-plane endpoint URL for a (community, server) scope. The
// path shape mirrors the API router prefix (the whole HTTP API is namespaced
// under /api, issue #498 — dataplane/api/transfers.py:
// /api/data-plane/communities/{c}/servers/{s}/...).
func scopeURL(base, community, server, suffix string) string {
	return base + "/api/data-plane/communities/" + community + "/servers/" + server + "/" + suffix
}

// TestSnapshotThenHydrateRoundTrip is the honest end-to-end flow: the client
// publishes a small working set (exercising pack + upload + the API's
// proven-complete publish), then hydrates it back into a fresh dir and compares
// bytes. This covers both transfer directions against the real endpoint without
// needing the full server-creation API.
func TestSnapshotThenHydrateRoundTrip(t *testing.T) {
	base := env(t, "MCD_E2E_API_URL")
	credential := env(t, "MCD_E2E_CREDENTIAL")

	// A fresh scope per run keeps reruns independent of any prior published set.
	community := "11111111-1111-1111-1111-111111111111"
	server := newServerID(t)

	client := datatransfer.New(httpClient())
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	// Arrange: a small working set on disk.
	src := t.TempDir()
	want := map[string]string{
		"server.properties": "motd=hello-e2e\n",
		"world/level.dat":   "level-bytes",
	}
	writeTree(t, src, want)

	// Act 1: snapshot (pack + upload + publish). A clean publish is 204. Base
	// generation 0 (never hydrated) skips the publish-time guard (#847); the worker
	// id is recorded as the publisher (#847 bug 3).
	snapshotURL := scopeURL(base, community, server, "snapshot")
	if _, err := client.Snapshot(ctx, snapshotURL, credential, src, 0, "e2e-worker", false); err != nil {
		t.Fatalf("Snapshot against real API: %v", err)
	}

	// Act 2: hydrate the just-published set back into a fresh dir.
	dest := t.TempDir()
	hydrateURL := scopeURL(base, community, server, "working-set")
	if _, err := client.Hydrate(ctx, hydrateURL, credential, dest); err != nil {
		t.Fatalf("Hydrate against real API: %v", err)
	}

	// Assert: every file round-tripped byte-for-byte.
	for rel, content := range want {
		got, err := os.ReadFile(filepath.Join(dest, filepath.FromSlash(rel)))
		if err != nil {
			t.Fatalf("read hydrated %s: %v", rel, err)
		}
		if string(got) != content {
			t.Fatalf("hydrated %s = %q, want %q", rel, got, content)
		}
	}
}

// TestHydrateRejectsWrongCredential proves the auth header is enforced end to
// end: a wrong credential is a 401, which the client surfaces as an error
// naming the unexpected status rather than silently treating it as an empty
// working set.
func TestHydrateRejectsWrongCredential(t *testing.T) {
	base := env(t, "MCD_E2E_API_URL")
	_ = env(t, "MCD_E2E_CREDENTIAL") // ensure the harness is enabled

	community := "11111111-1111-1111-1111-111111111111"
	server := newServerID(t)

	client := datatransfer.New(httpClient())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	hydrateURL := scopeURL(base, community, server, "working-set")
	_, err := client.Hydrate(ctx, hydrateURL, "wrong-credential", t.TempDir())
	if err == nil {
		t.Fatal("expected an error hydrating with a wrong credential (401)")
	}
	if !strings.Contains(err.Error(), "401") {
		t.Fatalf("expected the error to name the 401 status, got: %v", err)
	}
}

// httpClient returns a plain HTTP client; the harness talks to a local uvicorn
// over plaintext, matching the insecure-dev transport posture (CONFIGURATION.md
// Section 6.1) the wiring layer would otherwise build with TLS.
func httpClient() *http.Client {
	return &http.Client{Timeout: 60 * time.Second}
}

// writeTree materialises {relpath: content} under root, creating parent dirs.
func writeTree(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for rel, content := range files {
		full := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(full), 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(content), 0o640); err != nil {
			t.Fatal(err)
		}
	}
}

// newServerID returns a fresh RFC 4122 v4 server UUID so each run snapshots into
// its own scope, keeping reruns independent.
func newServerID(t *testing.T) string {
	t.Helper()
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		t.Fatalf("rand: %v", err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	const hex = "0123456789abcdef"
	out := make([]byte, 36)
	pos := 0
	for i, v := range b {
		if i == 4 || i == 6 || i == 8 || i == 10 {
			out[pos] = '-'
			pos++
		}
		out[pos] = hex[v>>4]
		out[pos+1] = hex[v&0x0f]
		pos += 2
	}
	return string(out)
}
