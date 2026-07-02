package config

import (
	"os"
	"path/filepath"
	"testing"
)

// noEnv is a getenv that always returns empty.
func noEnv(string) string { return "" }

// envMap builds a getenv from a map.
func envMap(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func writeTOML(t *testing.T, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "relay.toml")
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

const minimalTOML = `
[api]
grpc_endpoint = "api:50051"
credential = "secret"
[api.tls]
insecure = true
[tunnel]
public_endpoint = "relay.example.com:25665"
[tunnel.tls]
cert_file = "/tls/cert.pem"
key_file = "/tls/key.pem"
`

func TestLoadDefaults(t *testing.T) {
	cfg, err := Load(writeTOML(t, minimalTOML), noEnv)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.Game.Listen != ":25565" {
		t.Errorf("game.listen default = %q", cfg.Game.Listen)
	}
	if cfg.Game.StatusCacheSeconds != 5 {
		t.Errorf("status_cache_seconds default = %d", cfg.Game.StatusCacheSeconds)
	}
	if cfg.Game.MaxConnsPerIP != 32 || cfg.Game.JoinsPerIPPerSecond != 10 {
		t.Errorf("ip caps defaults = %d/%d", cfg.Game.MaxConnsPerIP, cfg.Game.JoinsPerIPPerSecond)
	}
	if cfg.Tunnel.Listen != ":25665" {
		t.Errorf("tunnel.listen default = %q", cfg.Tunnel.Listen)
	}
	if cfg.Tunnel.MaxConnsPerIP != 64 {
		t.Errorf("tunnel.max_conns_per_ip default = %d", cfg.Tunnel.MaxConnsPerIP)
	}
	if cfg.Bedrock.TunnelListen != ":25675" {
		t.Errorf("bedrock.tunnel_listen default = %q", cfg.Bedrock.TunnelListen)
	}
	if cfg.Bedrock.MaxFlowsPerIP != 32 || cfg.Bedrock.NewFlowsPerIPPerSecond != 10 {
		t.Errorf("bedrock ip caps defaults = %d/%d", cfg.Bedrock.MaxFlowsPerIP, cfg.Bedrock.NewFlowsPerIPPerSecond)
	}
	if cfg.Log.Level != "info" || cfg.Log.Format != "json" {
		t.Errorf("log defaults = %q/%q", cfg.Log.Level, cfg.Log.Format)
	}
}

func TestLoadEnvOverride(t *testing.T) {
	env := envMap(map[string]string{
		"MCD_RELAY_GAME_LISTEN":               ":30000",
		"MCD_RELAY_GAME_MAX_CONNS_PER_IP":     "8",
		"MCD_RELAY_GAME_STATUS_CACHE_SECONDS": "12",
		"MCD_RELAY_API_CREDENTIAL":            "envsecret",
		"MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT":    "other:25665",
		"MCD_RELAY_TUNNEL_MAX_CONNS_PER_IP":   "128",
		"MCD_RELAY_BEDROCK_TUNNEL_LISTEN":     ":30675",
		"MCD_RELAY_BEDROCK_MAX_FLOWS_PER_IP":  "16",
	})
	cfg, err := Load(writeTOML(t, minimalTOML), env)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.Game.Listen != ":30000" {
		t.Errorf("env override game.listen = %q", cfg.Game.Listen)
	}
	if cfg.Game.MaxConnsPerIP != 8 {
		t.Errorf("env override max_conns_per_ip = %d", cfg.Game.MaxConnsPerIP)
	}
	if cfg.Game.StatusCacheSeconds != 12 {
		t.Errorf("env override status_cache_seconds = %d", cfg.Game.StatusCacheSeconds)
	}
	if cfg.API.Credential != "envsecret" {
		t.Errorf("env override credential = %q", cfg.API.Credential)
	}
	if cfg.Tunnel.PublicEndpoint != "other:25665" {
		t.Errorf("env override public_endpoint = %q", cfg.Tunnel.PublicEndpoint)
	}
	if cfg.Tunnel.MaxConnsPerIP != 128 {
		t.Errorf("env override tunnel.max_conns_per_ip = %d", cfg.Tunnel.MaxConnsPerIP)
	}
	if cfg.Bedrock.TunnelListen != ":30675" {
		t.Errorf("env override bedrock.tunnel_listen = %q", cfg.Bedrock.TunnelListen)
	}
	if cfg.Bedrock.MaxFlowsPerIP != 16 {
		t.Errorf("env override bedrock.max_flows_per_ip = %d", cfg.Bedrock.MaxFlowsPerIP)
	}
}

func TestValidateMissingRequired(t *testing.T) {
	if _, err := Load("", noEnv); err == nil {
		t.Error("empty config should fail validation")
	}
}

func TestValidateTLSRequired(t *testing.T) {
	body := `
[api]
grpc_endpoint = "api:50051"
credential = "secret"
[tunnel]
public_endpoint = "relay:25665"
[tunnel.tls]
cert_file = "/c"
key_file = "/k"
`
	if _, err := Load(writeTOML(t, body), noEnv); err == nil {
		t.Error("missing api.tls.ca_file without insecure should fail")
	}
}

func TestValidateMissingTunnelTLS(t *testing.T) {
	body := `
[api]
grpc_endpoint = "api:50051"
credential = "secret"
[api.tls]
insecure = true
[tunnel]
public_endpoint = "relay:25665"
`
	if _, err := Load(writeTOML(t, body), noEnv); err == nil {
		t.Error("missing tunnel.tls cert/key should fail")
	}
}

func TestValidateBadLogFormat(t *testing.T) {
	env := envMap(map[string]string{"MCD_RELAY_LOG_FORMAT": "yaml"})
	if _, err := Load(writeTOML(t, minimalTOML), env); err == nil {
		t.Error("unknown log.format should fail")
	}
}
