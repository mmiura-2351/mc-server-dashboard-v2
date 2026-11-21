package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc/grpclog"

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

// TestEscapingWriterAddsNoAllocationToAPlainRecord pins the fast path: a record
// with nothing to escape is handed on as it is, so the writer adds no allocation
// to what slog itself makes.
func TestEscapingWriterAddsNoAllocationToAPlainRecord(t *testing.T) {
	if raceEnabled {
		t.Skip("the race detector makes sync.Pool drop items at random, so allocation counts are noise")
	}
	handlers := map[string]func(io.Writer) slog.Handler{
		"json": func(w io.Writer) slog.Handler { return slog.NewJSONHandler(w, nil) },
		"text": func(w io.Writer) slog.Handler { return slog.NewTextHandler(w, nil) },
	}
	for format, handler := range handlers {
		t.Run(format, func(t *testing.T) {
			allocs := func(w io.Writer) float64 {
				logger := slog.New(handler(w))
				return testing.AllocsPerRun(100, func() {
					logger.Info("server started", "server_id", "s-1", "port", 25565)
				})
			}
			if base, got := allocs(io.Discard), allocs(escapingWriter{io.Discard}); got != base {
				t.Errorf("allocations per record = %v through the writer, %v without", got, base)
			}
		})
	}
}

// hostile is text a log line must not carry raw (issue #3960): ESC, a C0
// control, which slog escapes itself, and U+202E and DEL, which escapingWriter
// escapes.
const hostile = "\x1b[31m\u202e\x7f"

// assertNoRawControls checks that out carries none of hostile's controls raw.
func assertNoRawControls(t *testing.T, out string) {
	t.Helper()
	for _, r := range []rune{0x1b, 0x202e, 0x7f} {
		if strings.ContainsRune(out, r) {
			t.Errorf("%U written raw: %q", r, out)
		}
	}
}

// jsonRecords decodes each line of out as a json record, and fails t for a line
// that is not one.
func jsonRecords(t *testing.T, out string) []map[string]any {
	t.Helper()
	var recs []map[string]any
	for line := range strings.Lines(out) {
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Errorf("not a json record: %q", line)
			continue
		}
		recs = append(recs, rec)
	}
	return recs
}

// hasRecord reports whether recs holds a record at level with message msg.
func hasRecord(recs []map[string]any, level, msg string) bool {
	return slices.ContainsFunc(recs, func(rec map[string]any) bool {
		return rec["level"] == level && rec["msg"] == msg
	})
}

// jsonErrorRecord returns the error of the json ERROR record msg in out, and
// whether out holds one. A line that is not json fails t.
func jsonErrorRecord(t *testing.T, out, msg string) (string, bool) {
	t.Helper()
	for _, rec := range jsonRecords(t, out) {
		if err, ok := rec["error"].(string); ok && rec["level"] == "ERROR" && rec["msg"] == msg {
			return err, true
		}
	}
	return "", false
}

// textErrorRecord returns the error of the text ERROR record msg in out,
// unquoted, and whether out holds one.
func textErrorRecord(out, msg string) (string, bool) {
	for line := range strings.Lines(out) {
		_, quoted, ok := strings.Cut(strings.TrimSuffix(line, "\n"), " level=ERROR msg="+strconv.Quote(msg)+" error=")
		if !ok {
			continue
		}
		if err, unquoteErr := strconv.Unquote(quoted); unquoteErr == nil {
			return err, true
		}
	}
	return "", false
}

// TestRouteLogsSendsStdlibAndGRPCLogsThroughTheHandler pins what main sets up
// before it dials (issue #3960): the standard log package's lines and grpc-go's
// are records of the configured format, escaped like any other, and grpc keeps
// its own default of writing only errors unless GRPC_GO_LOG_SEVERITY_LEVEL asks
// for more. It runs in a child, since it changes the process's loggers.
func TestRouteLogsSendsStdlibAndGRPCLogsThroughTheHandler(t *testing.T) {
	if isChild(t) {
		routeLogs(newLogger(config.LogConfig{Level: "debug", Format: os.Getenv("MCSD_TEST_LOG_FORMAT")}))
		log.Print("stdlib" + hostile)
		grpclog.Component("probe").Error("grpc error" + hostile)
		grpclog.Component("probe").Info("grpc info")
		return
	}
	for _, tc := range []struct {
		format, severity string
		wantInfo         bool
	}{
		{"json", "", false},
		{"text", "", false},
		{"json", "info", true},
	} {
		out, code := runChild(t, map[string]string{
			"MCSD_TEST_LOG_FORMAT":       tc.format,
			"GRPC_GO_LOG_SEVERITY_LEVEL": tc.severity,
		})
		if code != 0 {
			t.Errorf("%s: child exited %d", tc.format, code)
		}
		assertNoRawControls(t, out)
		var stdlib, grpcError, grpcInfo bool
		if tc.format == "json" {
			recs := jsonRecords(t, out)
			stdlib = hasRecord(recs, "INFO", "stdlib"+hostile)
			grpcError = hasRecord(recs, "ERROR", "[probe] grpc error"+hostile)
			grpcInfo = hasRecord(recs, "INFO", "[probe] grpc info")
		} else {
			stdlib = strings.Contains(out, " level=INFO msg="+strconv.Quote("stdlib"+hostile)+"\n")
			grpcError = strings.Contains(out, " level=ERROR msg="+strconv.Quote("[probe] grpc error"+hostile)+"\n")
			grpcInfo = strings.Contains(out, "grpc info")
		}
		if !stdlib || !grpcError || grpcInfo != tc.wantInfo {
			t.Errorf("log.format=%s GRPC_GO_LOG_SEVERITY_LEVEL=%q: stdlib record %v, grpc error "+
				"record %v, grpc info record %v; want true, true, %v. Output:\n%s",
				tc.format, tc.severity, stdlib, grpcError, grpcInfo, tc.wantInfo, out)
		}
	}
}

// relayEnv is a configuration Load accepts and run fails on after the dial: the
// tunnel certificate certFile does not exist. The advertised CA is "system", so
// nothing reads that file before the dial.
func relayEnv(certFile string) map[string]string {
	return map[string]string{
		"MCD_RELAY_API_GRPC_ENDPOINT":             "127.0.0.1:1",
		"MCD_RELAY_API_CREDENTIAL":                "test-credential",
		"MCD_RELAY_API_TLS_INSECURE":              "true",
		"MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT":        "relay.invalid:25665",
		"MCD_RELAY_TUNNEL_TLS_CERT_FILE":          certFile,
		"MCD_RELAY_TUNNEL_TLS_KEY_FILE":           certFile,
		"MCD_RELAY_TUNNEL_TLS_ADVERTISED_CA_FILE": "system",
	}
}

// TestMainSendsGRPCLogsThroughTheHandler pins that main sets up the grpc logger
// before it dials (issue #3960). Asked for its info lines, grpc logs the channel
// the dial creates; run then fails on a missing tunnel certificate, and each line
// written on the way is a json record, grpc's among them.
func TestMainSendsGRPCLogsThroughTheHandler(t *testing.T) {
	if isChild(t) {
		main()
		return
	}
	env := relayEnv(filepath.Join(t.TempDir(), "tunnel.pem"))
	env["GRPC_GO_LOG_SEVERITY_LEVEL"] = "info"
	out, code := runChild(t, env)
	if code != 1 {
		t.Errorf("exit code %d, want 1", code)
	}
	recs := jsonRecords(t, out)
	if !slices.ContainsFunc(recs, func(rec map[string]any) bool {
		msg, _ := rec["msg"].(string)
		return strings.HasPrefix(msg, "[core] ")
	}) {
		t.Errorf("no grpc record in:\n%s", out)
	}
}

// TestMainLogsAFatalErrorThroughTheConfiguredLogger: once main has built the
// logger, it logs the error it exits on through it, in the configured format
// (issue #3960): here a tunnel certificate whose path holds controls and which
// does not exist.
func TestMainLogsAFatalErrorThroughTheConfiguredLogger(t *testing.T) {
	if isChild(t) {
		main()
		return
	}
	certFile := filepath.Join(t.TempDir(), "tunnel"+hostile+".pem")
	for _, format := range []string{"json", "text"} {
		env := relayEnv(certFile)
		env["MCD_RELAY_LOG_FORMAT"] = format
		out, code := runChild(t, env)
		if code != 1 {
			t.Errorf("%s: exit code %d, want 1", format, code)
		}
		assertNoRawControls(t, out)
		err, ok := textErrorRecord(out, "relay failed")
		if format == "json" {
			err, ok = jsonErrorRecord(t, out, "relay failed")
		}
		if !ok || !strings.Contains(err, certFile) {
			t.Errorf("%s: no ERROR record \"relay failed\" naming %q in:\n%s", format, certFile, out)
		}
	}
}

// TestMainLogsAConfigErrorAsAnEscapedTextRecord: before a configuration is
// loaded there is no log.format to follow, so main writes the error it exits on
// as a text record (issue #3960), escaped like any other: here a config path
// that holds controls.
func TestMainLogsAConfigErrorAsAnEscapedTextRecord(t *testing.T) {
	if isChild(t) {
		main()
		return
	}
	path := filepath.Join(t.TempDir(), "relay"+hostile+".toml")
	out, code := runChild(t, map[string]string{configPathEnv: path})
	if code != 1 {
		t.Errorf("exit code %d, want 1", code)
	}
	assertNoRawControls(t, out)
	if err, ok := textErrorRecord(out, "relay failed"); !ok || !strings.Contains(err, path) {
		t.Errorf("no text ERROR record \"relay failed\" naming %q in:\n%s", path, out)
	}
}

// TestGRPCLoggerFatalLogsAnErrorAndExits keeps grpc's contract for Fatal: the
// record is an error, and the process exits 1 after it.
func TestGRPCLoggerFatalLogsAnErrorAndExits(t *testing.T) {
	if isChild(t) {
		g := newGRPCLogger(newLogger(config.LogConfig{}), os.Getenv)
		switch os.Getenv("MCSD_TEST_FATAL") {
		case "Fatal":
			g.Fatal("grpc fatal")
		case "Fatalln":
			g.Fatalln("grpc", "fatal")
		case "Fatalf":
			g.Fatalf("grpc %s", "fatal")
		}
		return
	}
	for _, call := range []string{"Fatal", "Fatalln", "Fatalf"} {
		out, code := runChild(t, map[string]string{"MCSD_TEST_FATAL": call})
		if code != 1 {
			t.Errorf("%s: exit code %d, want 1", call, code)
		}
		if !hasRecord(jsonRecords(t, out), "ERROR", "grpc fatal") {
			t.Errorf("%s: no ERROR record \"grpc fatal\" in:\n%s", call, out)
		}
	}
}

// TestMetricsServerLogsItsErrorsThroughTheHandler: the metrics server's own
// lines, here the accept error it retries after, are ERROR records of the
// relay's logger (issue #3960), not plain text on stderr.
func TestMetricsServerLogsItsErrorsThroughTheHandler(t *testing.T) {
	var buf bytes.Buffer
	srv := newMetricsServer(prometheus.NewRegistry(), slog.New(slog.NewJSONHandler(&buf, nil)))
	if err := srv.Serve(&failingListener{}); !errors.Is(err, errListenerGone) {
		t.Fatalf("Serve = %v, want %v", err, errListenerGone)
	}
	if !slices.ContainsFunc(jsonRecords(t, buf.String()), func(rec map[string]any) bool {
		msg, _ := rec["msg"].(string)
		return rec["level"] == "ERROR" && strings.HasPrefix(msg, "http: Accept error: accept failed")
	}) {
		t.Errorf("no ERROR record of the accept error in:\n%s", buf.String())
	}
}

// errListenerGone is failingListener's error for good.
var errListenerGone = errors.New("listener gone")

// failingListener fails its first Accept with a temporary error, which
// http.Server logs and retries, and every later one with errListenerGone, which
// ends Serve.
type failingListener struct{ accepts int }

func (l *failingListener) Accept() (net.Conn, error) {
	l.accepts++
	if l.accepts == 1 {
		return nil, temporaryError{}
	}
	return nil, errListenerGone
}

func (*failingListener) Close() error   { return nil }
func (*failingListener) Addr() net.Addr { return &net.TCPAddr{} }

// temporaryError is a net.Error that says to retry.
type temporaryError struct{}

func (temporaryError) Error() string   { return "accept failed" }
func (temporaryError) Timeout() bool   { return false }
func (temporaryError) Temporary() bool { return true }
