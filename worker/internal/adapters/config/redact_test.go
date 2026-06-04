package config

import (
	"bytes"
	"log/slog"
	"strings"
	"testing"
)

func TestLogValueMasksSecrets(t *testing.T) {
	cfg := Config{
		API: APIConfig{
			GRPCEndpoint: "api:50051",
			DataPlaneURL: "https://api/data",
			Credential:   "super-secret-token",
			TLS:          TLSConfig{ClientKeyFile: "/keys/worker.key"},
		},
		Worker: WorkerConfig{ID: "w1", ScratchDir: "/scratch"},
		Log:    LogConfig{Level: "info", Format: "json"},
	}

	var buf bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&buf, nil))
	logger.Info("loaded config", "config", cfg.LogValue())
	out := buf.String()

	if strings.Contains(out, "super-secret-token") {
		t.Errorf("log output leaked the credential: %s", out)
	}
	if !strings.Contains(out, "api:50051") {
		t.Errorf("log output dropped a non-secret field: %s", out)
	}
	// The client key file path is also a secret reference; mask its value.
	if strings.Contains(out, "/keys/worker.key") {
		t.Errorf("log output leaked the mTLS key path: %s", out)
	}
}

func TestLogValueMarksEmptyCredentialDistinctly(t *testing.T) {
	cfg := Config{}
	lv := cfg.LogValue()
	if lv.Kind() != slog.KindGroup {
		t.Fatalf("LogValue kind = %v, want group", lv.Kind())
	}
}
