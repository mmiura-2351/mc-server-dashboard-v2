package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
)

// grpcLogger is the grpclog.LoggerV2 that writes grpc-go's lines through slog
// (issue #3960): Info, Warning and Error at their slog levels, and Fatal at
// Error before it exits. It keeps the severity and verbosity grpc's own logger
// would read from the environment, so by default only errors are written;
// log.level then filters what is left.
type grpcLogger struct {
	logger *slog.Logger
	// min is the lowest level written.
	min       slog.Level
	verbosity int
}

// newGRPCLogger reads GRPC_GO_LOG_SEVERITY_LEVEL and GRPC_GO_LOG_VERBOSITY_LEVEL
// through getenv as grpc-go's default logger does (grpclog's newLoggerV2, which
// is not exported): errors when the severity is unset or error, warnings too at
// warning, everything at info, and nothing at all for any other value.
func newGRPCLogger(logger *slog.Logger, getenv func(string) string) grpcLogger {
	g := grpcLogger{logger: logger, min: slog.LevelError + 1}
	switch getenv("GRPC_GO_LOG_SEVERITY_LEVEL") {
	case "", "ERROR", "error":
		g.min = slog.LevelError
	case "WARNING", "warning":
		g.min = slog.LevelWarn
	case "INFO", "info":
		g.min = slog.LevelInfo
	}
	if v, err := strconv.Atoi(getenv("GRPC_GO_LOG_VERBOSITY_LEVEL")); err == nil {
		g.verbosity = v
	}
	return g
}

func (g grpcLogger) Info(args ...any)                    { g.logSprint(slog.LevelInfo, args) }
func (g grpcLogger) Infoln(args ...any)                  { g.logSprintln(slog.LevelInfo, args) }
func (g grpcLogger) Infof(format string, args ...any)    { g.logSprintf(slog.LevelInfo, format, args) }
func (g grpcLogger) Warning(args ...any)                 { g.logSprint(slog.LevelWarn, args) }
func (g grpcLogger) Warningln(args ...any)               { g.logSprintln(slog.LevelWarn, args) }
func (g grpcLogger) Warningf(format string, args ...any) { g.logSprintf(slog.LevelWarn, format, args) }
func (g grpcLogger) Error(args ...any)                   { g.logSprint(slog.LevelError, args) }
func (g grpcLogger) Errorln(args ...any)                 { g.logSprintln(slog.LevelError, args) }
func (g grpcLogger) Errorf(format string, args ...any)   { g.logSprintf(slog.LevelError, format, args) }

func (g grpcLogger) Fatal(args ...any) {
	g.logSprint(slog.LevelError, args)
	os.Exit(1)
}

func (g grpcLogger) Fatalln(args ...any) {
	g.logSprintln(slog.LevelError, args)
	os.Exit(1)
}

func (g grpcLogger) Fatalf(format string, args ...any) {
	g.logSprintf(slog.LevelError, format, args)
	os.Exit(1)
}

func (g grpcLogger) V(l int) bool { return l <= g.verbosity }

// logSprint, logSprintln and logSprintf write one record when its severity is
// written, and format it only then, as grpc's own logger does.
func (g grpcLogger) logSprint(level slog.Level, args []any) {
	if level >= g.min {
		g.logger.Log(context.Background(), level, fmt.Sprint(args...))
	}
}

// logSprintln drops the line break fmt.Sprintln ends with: a record is one line.
func (g grpcLogger) logSprintln(level slog.Level, args []any) {
	if level >= g.min {
		g.logger.Log(context.Background(), level, strings.TrimSuffix(fmt.Sprintln(args...), "\n"))
	}
}

func (g grpcLogger) logSprintf(level slog.Level, format string, args []any) {
	if level >= g.min {
		g.logger.Log(context.Background(), level, fmt.Sprintf(format, args...))
	}
}
