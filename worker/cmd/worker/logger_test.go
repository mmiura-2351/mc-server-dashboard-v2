package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"google.golang.org/grpc/grpclog"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/config"
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
func logLine(t *testing.T, format, s string) string {
	t.Helper()
	read := captureStderr(t)
	newLogger(config.LogConfig{Format: format}).Info("a"+s, "k"+s, "v"+s, "err", errors.New("e"+s))
	out := read()
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
// before it dials (issue #3960): the standard log package's lines, at WARN, and
// grpc-go's are records of the configured format, escaped like any other, and
// grpc keeps its own default of writing only errors unless
// GRPC_GO_LOG_SEVERITY_LEVEL asks for more. It runs in a child, since it changes
// the process's loggers.
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
			stdlib = hasRecord(recs, "WARN", "stdlib"+hostile)
			grpcError = hasRecord(recs, "ERROR", "[probe] grpc error"+hostile)
			grpcInfo = hasRecord(recs, "INFO", "[probe] grpc info")
		} else {
			stdlib = strings.Contains(out, " level=WARN msg="+strconv.Quote("stdlib"+hostile)+"\n")
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

// TestMainSendsGRPCLogsThroughTheHandler pins that main sets up the grpc logger,
// over the configured handler, before it dials (issue #3960). Asked for its info
// lines, grpc logs the channel the dial creates; boot then fails on an
// unsupported docker host, and each line written on the way is a record of the
// configured format, grpc's among them.
func TestMainSendsGRPCLogsThroughTheHandler(t *testing.T) {
	if isChild(t) {
		main()
		return
	}
	for _, format := range []string{"json", "text"} {
		env := bootEnv(t.TempDir(), "tcp://127.0.0.1:2375", "127.0.0.1:1")
		env["MCD_WORKER_LOG_LEVEL"] = "info"
		env["MCD_WORKER_LOG_FORMAT"] = format
		env["GRPC_GO_LOG_SEVERITY_LEVEL"] = "info"
		out, code := runChild(t, env)
		if code != 1 {
			t.Errorf("%s: exit code %d, want 1", format, code)
		}
		var grpcRecord bool
		if format == "json" {
			grpcRecord = slices.ContainsFunc(jsonRecords(t, out), func(rec map[string]any) bool {
				msg, _ := rec["msg"].(string)
				return rec["level"] == "INFO" && strings.HasPrefix(msg, "[core] ")
			})
		} else {
			for line := range strings.Lines(out) {
				if !strings.HasPrefix(line, "time=") {
					t.Errorf("text: not a text record: %q", line)
				}
				grpcRecord = grpcRecord || strings.Contains(line, ` level=INFO msg="[core] `)
			}
		}
		if !grpcRecord {
			t.Errorf("%s: no grpc record in:\n%s", format, out)
		}
	}
}

// TestMainLogsAFatalErrorThroughTheConfiguredLogger: once main has built the
// logger, it logs the error it exits on through it, in the configured format
// (issue #3960): here a dial that cannot read a CA file whose path holds
// controls.
func TestMainLogsAFatalErrorThroughTheConfiguredLogger(t *testing.T) {
	if isChild(t) {
		main()
		return
	}
	caFile := filepath.Join(t.TempDir(), "ca"+hostile+".pem")
	for _, format := range []string{"json", "text"} {
		env := bootEnv(t.TempDir(), "unix:///no-such-docker.sock", "127.0.0.1:1")
		env["MCD_WORKER_API_TLS_INSECURE"] = "false"
		env["MCD_WORKER_API_TLS_CA_FILE"] = caFile
		env["MCD_WORKER_LOG_FORMAT"] = format
		out, code := runChild(t, env)
		if code != 1 {
			t.Errorf("%s: exit code %d, want 1", format, code)
		}
		assertNoRawControls(t, out)
		err, ok := textErrorRecord(out, "worker failed")
		if format == "json" {
			err, ok = jsonErrorRecord(t, out, "worker failed")
		}
		if !ok || !strings.Contains(err, caFile) {
			t.Errorf("%s: no ERROR record \"worker failed\" naming %q in:\n%s", format, caFile, out)
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
	path := filepath.Join(t.TempDir(), "worker"+hostile+".toml")
	out, code := runChild(t, map[string]string{configPathEnv: path})
	if code != 1 {
		t.Errorf("exit code %d, want 1", code)
	}
	assertNoRawControls(t, out)
	if err, ok := textErrorRecord(out, "worker failed"); !ok || !strings.Contains(err, path) {
		t.Errorf("no text ERROR record \"worker failed\" naming %q in:\n%s", path, out)
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
