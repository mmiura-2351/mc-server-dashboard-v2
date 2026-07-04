// Package config loads the relay's runtime configuration. It mirrors the
// Worker's config adapter (worker/internal/adapters/config): reading
// configuration is an edge concern, so the wiring layer (cmd/relay) reads it
// and injects already-constructed values inward (docs/app/RELAY.md Section 13,
// docs/app/CONFIGURATION.md Section 1).
//
// Precedence matches the Worker and CONFIGURATION.md Section 2:
//
//	defaults (in code) < config file (TOML) < environment variables
//
// Environment variables use the MCD_RELAY_ prefix; the logical key path is
// upper-cased with dots replaced by underscores (e.g. api.grpc_endpoint becomes
// MCD_RELAY_API_GRPC_ENDPOINT). A required key missing from every source, or a
// malformed value, is a fatal startup error. Secret values are never logged;
// see Config.LogValue.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"

	"github.com/BurntSushi/toml"
)

// EnvPrefix is the environment-variable prefix for every relay key
// (RELAY.md Section 13).
const EnvPrefix = "MCD_RELAY_"

// Config is the relay's resolved configuration (RELAY.md Section 13).
type Config struct {
	API     APIConfig
	Game    GameConfig
	Tunnel  TunnelConfig
	Bedrock BedrockConfig
	Log     LogConfig
}

// APIConfig is the API connection and authentication surface. Same shape as the
// Worker's API connection config (RELAY.md Section 13).
type APIConfig struct {
	// GRPCEndpoint is the API control-plane gRPC address the relay dials.
	GRPCEndpoint string
	// Credential authenticates the relay to the API. Secret: never logged.
	Credential string
	// TLS holds the control-channel TLS material.
	TLS TLSConfig
}

// TLSConfig is the control-channel TLS material (same posture as the Worker).
type TLSConfig struct {
	// CAFile is the CA bundle verifying the API's TLS. When set, the relay dials
	// with TLS verified against it.
	CAFile string
	// Insecure opts in to a plaintext (no-TLS) dial. Honoured only when CAFile is
	// empty; for local/dev use. With neither set, validation fails fast.
	Insecure bool
}

// GameConfig is the public player listener surface (RELAY.md Sections 11, 13).
type GameConfig struct {
	// Listen is the public player listener address (default :25565).
	Listen string
	// StatusCacheSeconds is the per-slug status-ping cache TTL (default 5).
	StatusCacheSeconds uint32
	// StatusCacheMaxEntries caps the number of entries in the status cache
	// (default 1024). When exceeded, the oldest entry is evicted.
	StatusCacheMaxEntries uint32
	// MaxConnsPerIP caps concurrent connections from one source IP (default 32).
	MaxConnsPerIP uint32
	// JoinsPerIPPerSecond caps the join rate from one source IP (default 10).
	JoinsPerIPPerSecond uint32
}

// TunnelConfig is the Worker dial-back listener surface (RELAY.md Sections 5,
// 12). The listener is always TLS.
type TunnelConfig struct {
	// Listen is the Worker dial-back listener address (default :25665).
	Listen string
	// PublicEndpoint is the host:port advertised to Workers via Register →
	// TunnelDial so they know where to dial back. Required.
	PublicEndpoint string
	// MaxConnsPerIP caps concurrent connections from one source IP on the tunnel
	// listener (default 64), bounding how many pre-auth handshake windows one IP
	// can hold (RELAY.md Section 11).
	MaxConnsPerIP uint32
	// TLS is the tunnel listener TLS material. Required.
	TLS TunnelTLSConfig
}

// BedrockConfig is the Bedrock tunnel QUIC listener surface (epic #1540,
// issue #1545; docs/app/BEDROCK_TUNNEL.md). The listener reuses the tunnel
// listener's TLS certificate (TunnelConfig.TLS) -- a distinct ALPN
// distinguishes the two on the wire -- so there is no separate cert/key pair
// here.
type BedrockConfig struct {
	// Enabled toggles the Bedrock QUIC/UDP tunnel listener (default false).
	// Off by default so a Java-only relay neither binds nor requires the
	// Bedrock UDP ports (bedrock.tunnel_listen and the per-tunnel bedrock_port
	// window) -- a host-port conflict on either must not take Java joins down
	// on upgrade (issue #1584). Mirrors the API's relay.bedrock_enabled /
	// MCD_API_RELAY__BEDROCK_ENABLED; compose wires both services from the
	// same operator setting.
	Enabled bool
	// TunnelListen is the public QUIC/UDP address Workers dial to open a
	// Bedrock tunnel (default :25675). This mirrors the API-side
	// relay.bedrock_tunnel_port default (docs/app/CONFIGURATION.md Section
	// 5.13): both are operator-configured, kept in sync like
	// game.listen/tunnel.listen already are with relay.game_port/tunnel_port.
	TunnelListen string
	// TunnelMaxConnsPerIP caps concurrent unauthenticated handshake windows
	// per source IP on the Bedrock tunnel QUIC listener (default 64) -- the
	// same posture as tunnel.max_conns_per_ip on the TCP tunnel listener
	// (issue #968; docs/app/BEDROCK_TUNNEL.md Section 8).
	TunnelMaxConnsPerIP uint32
	// MaxFlowsPerIP caps concurrent Bedrock client flows (by source address)
	// per source IP on one bound bedrock_port (default 32). Hygiene against
	// RakNet unconnected-ping amplification/spam on the public UDP ingress
	// (docs/app/BEDROCK_TUNNEL.md).
	MaxFlowsPerIP uint32
	// NewFlowsPerIPPerSecond caps the rate of new flows per source IP on one
	// bound bedrock_port (default 10).
	NewFlowsPerIPPerSecond uint32
}

// TunnelTLSConfig is the tunnel listener's server certificate material.
type TunnelTLSConfig struct {
	// CertFile is the tunnel listener's TLS certificate. Required.
	CertFile string
	// KeyFile is the tunnel listener's TLS private key. Required. Secret.
	KeyFile string
	// AdvertisedCAFile controls the CA bundle the relay advertises to Workers
	// (Register → TunnelDial) for verifying the tunnel certificate:
	//   - unset (empty): derive from CertFile (the self-signed default — the
	//     listener cert IS the chain Workers verify against).
	//   - SystemRootsCA ("system"): advertise an empty bundle, so Workers verify
	//     against their system roots (the cert is issued by a public CA).
	//   - any other value: a path to a PEM bundle to advertise verbatim.
	AdvertisedCAFile string
}

// SystemRootsCA is the sentinel value for tunnel.tls.advertised_ca_file that
// advertises an empty CA bundle (Workers fall back to system roots).
const SystemRootsCA = "system"

// LogConfig is the observability surface, identical to the Worker's.
type LogConfig struct {
	Level  string
	Format string
}

// fileConfig mirrors Config with TOML tags so the file form nests each logical
// key under its group. Pointers distinguish "unset" (keep the default) from a
// zero value the file explicitly supplied.
type fileConfig struct {
	API struct {
		GRPCEndpoint *string `toml:"grpc_endpoint"`
		Credential   *string `toml:"credential"`
		TLS          struct {
			CAFile   *string `toml:"ca_file"`
			Insecure *bool   `toml:"insecure"`
		} `toml:"tls"`
	} `toml:"api"`
	Game struct {
		Listen                *string `toml:"listen"`
		StatusCacheSeconds    *uint32 `toml:"status_cache_seconds"`
		StatusCacheMaxEntries *uint32 `toml:"status_cache_max_entries"`
		MaxConnsPerIP         *uint32 `toml:"max_conns_per_ip"`
		JoinsPerIPPerSecond   *uint32 `toml:"joins_per_ip_per_second"`
	} `toml:"game"`
	Tunnel struct {
		Listen         *string `toml:"listen"`
		PublicEndpoint *string `toml:"public_endpoint"`
		MaxConnsPerIP  *uint32 `toml:"max_conns_per_ip"`
		TLS            struct {
			CertFile         *string `toml:"cert_file"`
			KeyFile          *string `toml:"key_file"`
			AdvertisedCAFile *string `toml:"advertised_ca_file"`
		} `toml:"tls"`
	} `toml:"tunnel"`
	Bedrock struct {
		Enabled                *bool   `toml:"enabled"`
		TunnelListen           *string `toml:"tunnel_listen"`
		TunnelMaxConnsPerIP    *uint32 `toml:"tunnel_max_conns_per_ip"`
		MaxFlowsPerIP          *uint32 `toml:"max_flows_per_ip"`
		NewFlowsPerIPPerSecond *uint32 `toml:"new_flows_per_ip_per_second"`
	} `toml:"bedrock"`
	Log struct {
		Level  *string `toml:"level"`
		Format *string `toml:"format"`
	} `toml:"log"`
}

// defaults returns a Config holding the in-code default values (RELAY.md
// Section 12). Keys with no default stay zero and are checked in validate.
func defaults() Config {
	return Config{
		Game: GameConfig{
			Listen:                ":25565",
			StatusCacheSeconds:    5,
			StatusCacheMaxEntries: 1024,
			MaxConnsPerIP:         32,
			JoinsPerIPPerSecond:   10,
		},
		Tunnel: TunnelConfig{
			Listen:        ":25665",
			MaxConnsPerIP: 64,
		},
		Bedrock: BedrockConfig{
			TunnelListen:           ":25675",
			TunnelMaxConnsPerIP:    64,
			MaxFlowsPerIP:          32,
			NewFlowsPerIPPerSecond: 10,
		},
		Log: LogConfig{
			Level:  "info",
			Format: "json",
		},
	}
}

// Load resolves the configuration from defaults, an optional TOML file, and
// environment variables (in that precedence), then validates it. An empty path
// skips the file layer. A required key missing everywhere or a malformed value
// is a fatal error the caller surfaces at boot.
func Load(path string, getenv func(string) string) (Config, error) {
	cfg := defaults()

	if path != "" {
		if err := applyFile(&cfg, path); err != nil {
			return Config{}, err
		}
	}

	if err := applyEnv(&cfg, getenv); err != nil {
		return Config{}, err
	}

	if err := cfg.validate(); err != nil {
		return Config{}, err
	}

	return cfg, nil
}

// applyFile overlays the TOML file onto cfg. Only keys present in the file
// override the defaults already in cfg.
func applyFile(cfg *Config, path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("config: read file %q: %w", path, err)
	}

	var fc fileConfig
	if err := toml.Unmarshal(data, &fc); err != nil {
		return fmt.Errorf("config: parse file %q: %w", path, err)
	}

	setString(&cfg.API.GRPCEndpoint, fc.API.GRPCEndpoint)
	setString(&cfg.API.Credential, fc.API.Credential)
	setString(&cfg.API.TLS.CAFile, fc.API.TLS.CAFile)
	if fc.API.TLS.Insecure != nil {
		cfg.API.TLS.Insecure = *fc.API.TLS.Insecure
	}
	setString(&cfg.Game.Listen, fc.Game.Listen)
	setUint32(&cfg.Game.StatusCacheSeconds, fc.Game.StatusCacheSeconds)
	setUint32(&cfg.Game.StatusCacheMaxEntries, fc.Game.StatusCacheMaxEntries)
	setUint32(&cfg.Game.MaxConnsPerIP, fc.Game.MaxConnsPerIP)
	setUint32(&cfg.Game.JoinsPerIPPerSecond, fc.Game.JoinsPerIPPerSecond)
	setString(&cfg.Tunnel.Listen, fc.Tunnel.Listen)
	setString(&cfg.Tunnel.PublicEndpoint, fc.Tunnel.PublicEndpoint)
	setUint32(&cfg.Tunnel.MaxConnsPerIP, fc.Tunnel.MaxConnsPerIP)
	setString(&cfg.Tunnel.TLS.CertFile, fc.Tunnel.TLS.CertFile)
	setString(&cfg.Tunnel.TLS.KeyFile, fc.Tunnel.TLS.KeyFile)
	setString(&cfg.Tunnel.TLS.AdvertisedCAFile, fc.Tunnel.TLS.AdvertisedCAFile)
	if fc.Bedrock.Enabled != nil {
		cfg.Bedrock.Enabled = *fc.Bedrock.Enabled
	}
	setString(&cfg.Bedrock.TunnelListen, fc.Bedrock.TunnelListen)
	setUint32(&cfg.Bedrock.TunnelMaxConnsPerIP, fc.Bedrock.TunnelMaxConnsPerIP)
	setUint32(&cfg.Bedrock.MaxFlowsPerIP, fc.Bedrock.MaxFlowsPerIP)
	setUint32(&cfg.Bedrock.NewFlowsPerIPPerSecond, fc.Bedrock.NewFlowsPerIPPerSecond)
	setString(&cfg.Log.Level, fc.Log.Level)
	setString(&cfg.Log.Format, fc.Log.Format)

	return nil
}

// applyEnv overlays MCD_RELAY_ environment variables onto cfg (highest
// precedence). Each known key reads its prefixed, upper-cased name.
func applyEnv(cfg *Config, getenv func(string) string) error {
	setEnvString(&cfg.API.GRPCEndpoint, getenv, "API_GRPC_ENDPOINT")
	setEnvString(&cfg.API.Credential, getenv, "API_CREDENTIAL")
	setEnvString(&cfg.API.TLS.CAFile, getenv, "API_TLS_CA_FILE")
	if v := getenv(EnvPrefix + "API_TLS_INSECURE"); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			return fmt.Errorf("config: %sAPI_TLS_INSECURE: %w", EnvPrefix, err)
		}
		cfg.API.TLS.Insecure = b
	}
	setEnvString(&cfg.Game.Listen, getenv, "GAME_LISTEN")
	if err := setEnvUint32(&cfg.Game.StatusCacheSeconds, getenv, "GAME_STATUS_CACHE_SECONDS"); err != nil {
		return err
	}
	if err := setEnvUint32(&cfg.Game.StatusCacheMaxEntries, getenv, "GAME_STATUS_CACHE_MAX_ENTRIES"); err != nil {
		return err
	}
	if err := setEnvUint32(&cfg.Game.MaxConnsPerIP, getenv, "GAME_MAX_CONNS_PER_IP"); err != nil {
		return err
	}
	if err := setEnvUint32(&cfg.Game.JoinsPerIPPerSecond, getenv, "GAME_JOINS_PER_IP_PER_SECOND"); err != nil {
		return err
	}
	setEnvString(&cfg.Tunnel.Listen, getenv, "TUNNEL_LISTEN")
	setEnvString(&cfg.Tunnel.PublicEndpoint, getenv, "TUNNEL_PUBLIC_ENDPOINT")
	if err := setEnvUint32(&cfg.Tunnel.MaxConnsPerIP, getenv, "TUNNEL_MAX_CONNS_PER_IP"); err != nil {
		return err
	}
	setEnvString(&cfg.Tunnel.TLS.CertFile, getenv, "TUNNEL_TLS_CERT_FILE")
	setEnvString(&cfg.Tunnel.TLS.KeyFile, getenv, "TUNNEL_TLS_KEY_FILE")
	setEnvString(&cfg.Tunnel.TLS.AdvertisedCAFile, getenv, "TUNNEL_TLS_ADVERTISED_CA_FILE")
	if v := getenv(EnvPrefix + "BEDROCK_ENABLED"); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			return fmt.Errorf("config: %sBEDROCK_ENABLED: %w", EnvPrefix, err)
		}
		cfg.Bedrock.Enabled = b
	}
	setEnvString(&cfg.Bedrock.TunnelListen, getenv, "BEDROCK_TUNNEL_LISTEN")
	if err := setEnvUint32(&cfg.Bedrock.TunnelMaxConnsPerIP, getenv, "BEDROCK_TUNNEL_MAX_CONNS_PER_IP"); err != nil {
		return err
	}
	if err := setEnvUint32(&cfg.Bedrock.MaxFlowsPerIP, getenv, "BEDROCK_MAX_FLOWS_PER_IP"); err != nil {
		return err
	}
	if err := setEnvUint32(&cfg.Bedrock.NewFlowsPerIPPerSecond, getenv, "BEDROCK_NEW_FLOWS_PER_IP_PER_SECOND"); err != nil {
		return err
	}
	setEnvString(&cfg.Log.Level, getenv, "LOG_LEVEL")
	setEnvString(&cfg.Log.Format, getenv, "LOG_FORMAT")

	return nil
}

// validate enforces the required keys with no default and the documented value
// sets. Transport security is required unless explicitly opted out, mirroring
// the Worker (RELAY.md Section 13).
func (c Config) validate() error {
	var missing []string
	if c.API.GRPCEndpoint == "" {
		missing = append(missing, "api.grpc_endpoint")
	}
	if c.API.Credential == "" {
		missing = append(missing, "api.credential")
	}
	if c.Tunnel.PublicEndpoint == "" {
		missing = append(missing, "tunnel.public_endpoint")
	}
	if c.Tunnel.TLS.CertFile == "" {
		missing = append(missing, "tunnel.tls.cert_file")
	}
	if c.Tunnel.TLS.KeyFile == "" {
		missing = append(missing, "tunnel.tls.key_file")
	}
	if len(missing) > 0 {
		return fmt.Errorf("config: missing required key(s): %s", strings.Join(missing, ", "))
	}

	if c.API.TLS.CAFile == "" && !c.API.TLS.Insecure {
		return fmt.Errorf("config: api.tls.ca_file is required (or set api.tls.insecure=true for a plaintext dev dial)")
	}

	switch c.Log.Format {
	case "json", "text":
	default:
		return fmt.Errorf("config: log.format: unknown format %q (want json or text)", c.Log.Format)
	}

	return nil
}

func setString(dst, src *string) {
	if src != nil {
		*dst = *src
	}
}

func setUint32(dst, src *uint32) {
	if src != nil {
		*dst = *src
	}
}

func setEnvString(dst *string, getenv func(string) string, key string) {
	if v := getenv(EnvPrefix + key); v != "" {
		*dst = v
	}
}

func setEnvUint32(dst *uint32, getenv func(string) string, key string) error {
	if v := getenv(EnvPrefix + key); v != "" {
		n, err := strconv.ParseUint(v, 10, 32)
		if err != nil {
			return fmt.Errorf("config: %s%s: %w", EnvPrefix, key, err)
		}
		*dst = uint32(n)
	}
	return nil
}
