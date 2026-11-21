package main

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"slices"
	"strings"
	"testing"
)

// newTestGRPCLogger returns a grpcLogger whose environment is env, writing json
// records at any slog level, and the func that returns each record's level and
// message as "LEVEL msg".
func newTestGRPCLogger(t *testing.T, env map[string]string) (grpcLogger, func() []string) {
	t.Helper()
	var buf bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))
	g := newGRPCLogger(logger, func(k string) string { return env[k] })
	return g, func() []string {
		var got []string
		for line := range strings.Lines(buf.String()) {
			var rec struct{ Level, Msg string }
			if err := json.Unmarshal([]byte(line), &rec); err != nil {
				t.Fatalf("not a json record: %q", line)
			}
			got = append(got, rec.Level+" "+rec.Msg)
		}
		return got
	}
}

// TestGRPCLoggerKeepsGRPCSeverity: GRPC_GO_LOG_SEVERITY_LEVEL picks the lowest
// grpc severity written, read as grpc-go's own logger reads it: errors only when
// it is unset, and nothing at all for a value grpc does not know. Each severity
// is logged in all three forms.
func TestGRPCLoggerKeepsGRPCSeverity(t *testing.T) {
	info := []string{"INFO i", "INFO i", "INFO i"}
	warn := []string{"WARN w", "WARN w", "WARN w"}
	errs := []string{"ERROR e", "ERROR e", "ERROR e"}
	for severity, want := range map[string][]string{
		"":        errs,
		"ERROR":   errs,
		"error":   errs,
		"WARNING": slices.Concat(warn, errs),
		"warning": slices.Concat(warn, errs),
		"INFO":    slices.Concat(info, warn, errs),
		"info":    slices.Concat(info, warn, errs),
		"debug":   nil,
	} {
		t.Run("severity="+severity, func(t *testing.T) {
			g, records := newTestGRPCLogger(t, map[string]string{"GRPC_GO_LOG_SEVERITY_LEVEL": severity})
			g.Info("i")
			g.Infoln("i")
			g.Infof("i")
			g.Warning("w")
			g.Warningln("w")
			g.Warningf("w")
			g.Error("e")
			g.Errorln("e")
			g.Errorf("e")
			if got := records(); !slices.Equal(got, want) {
				t.Errorf("records %q, want %q", got, want)
			}
		})
	}
}

// TestGRPCLoggerWritesEachCallAtItsLevel: each method writes at its severity's
// slog level, its arguments formatted as fmt.Print, Println (without the line
// break) or Printf would.
func TestGRPCLoggerWritesEachCallAtItsLevel(t *testing.T) {
	g, records := newTestGRPCLogger(t, map[string]string{"GRPC_GO_LOG_SEVERITY_LEVEL": "info"})
	g.Info("a", 1, 2, "b")
	g.Infoln("a", 1, 2, "b")
	g.Infof("%s-%d", "a", 1)
	g.Warning("a", 1, 2, "b")
	g.Warningln("a", 1, 2, "b")
	g.Warningf("%s-%d", "a", 1)
	g.Error("a", 1, 2, "b")
	g.Errorln("a", 1, 2, "b")
	g.Errorf("%s-%d", "a", 1)
	want := []string{
		"INFO a1 2b", "INFO a 1 2 b", "INFO a-1",
		"WARN a1 2b", "WARN a 1 2 b", "WARN a-1",
		"ERROR a1 2b", "ERROR a 1 2 b", "ERROR a-1",
	}
	if got := records(); !slices.Equal(got, want) {
		t.Errorf("records %q, want %q", got, want)
	}
}

// TestGRPCLoggerKeepsGRPCVerbosity: V reports the verbosity
// GRPC_GO_LOG_VERBOSITY_LEVEL sets, 0 when it is unset or not an int.
func TestGRPCLoggerKeepsGRPCVerbosity(t *testing.T) {
	for _, tc := range []struct {
		verbosity string
		level     int
		want      bool
	}{
		{"", 0, true},
		{"", 1, false},
		{"2", 2, true},
		{"2", 3, false},
		{"two", 1, false},
		{"99999999999999999999", 1, false},
	} {
		g, _ := newTestGRPCLogger(t, map[string]string{"GRPC_GO_LOG_VERBOSITY_LEVEL": tc.verbosity})
		if got := g.V(tc.level); got != tc.want {
			t.Errorf("GRPC_GO_LOG_VERBOSITY_LEVEL=%q: V(%d) = %v, want %v", tc.verbosity, tc.level, got, tc.want)
		}
	}
}
