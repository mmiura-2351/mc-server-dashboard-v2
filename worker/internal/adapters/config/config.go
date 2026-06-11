// Package config loads the Worker's runtime configuration. It lives in the
// adapters layer because reading configuration is an edge concern
// (docs/app/CONFIGURATION.md Section 1): the wiring layer reads it and injects
// already-constructed values into the domain/application layers.
//
// Precedence mirrors the API side and CONFIGURATION.md Section 2:
//
//	defaults (in code) < config file (TOML) < environment variables
//
// Environment variables use the MCD_WORKER_ prefix; the logical key path is
// upper-cased with dots replaced by underscores (e.g. api.grpc_endpoint becomes
// MCD_WORKER_API_GRPC_ENDPOINT). A required key missing from every source, or a
// malformed value, is a fatal startup error (CONFIGURATION.md Section 2). Secret
// values are never logged; see Config.Redacted.
package config

import (
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"

	"github.com/BurntSushi/toml"
)

// EnvPrefix is the environment-variable prefix for every Worker key
// (CONFIGURATION.md Section 2).
const EnvPrefix = "MCD_WORKER_"

// Config is the Worker's resolved configuration. Only the keys this milestone
// needs are modelled (CONFIGURATION.md Section 6); the rest land with their
// features.
type Config struct {
	API    APIConfig
	Worker WorkerConfig
	Driver DriverConfig
	Log    LogConfig
}

// APIConfig is the API connection and authentication surface
// (CONFIGURATION.md Section 6.1).
type APIConfig struct {
	// GRPCEndpoint is the API control-plane gRPC address the Worker dials.
	GRPCEndpoint string
	// Credential authenticates the Worker to the API. Secret: never logged.
	Credential string
	// TLS holds the control-channel TLS material.
	TLS TLSConfig
}

// TLSConfig is the control-channel TLS material (CONFIGURATION.md Section 6.1).
type TLSConfig struct {
	// CAFile is the CA bundle verifying the API's TLS. When set, the Worker dials
	// with TLS verified against it.
	CAFile string
	// Insecure opts in to a plaintext (no-TLS) dial. It is only honoured when
	// CAFile is empty, and is for local/dev use only; production must set CAFile.
	// With neither CAFile nor Insecure, config validation fails fast.
	Insecure bool
	// ClientCertFile is the Worker's mTLS client certificate.
	ClientCertFile string
	// ClientKeyFile is the Worker's mTLS private key. Secret: never logged.
	ClientKeyFile string
}

// WorkerConfig is identity, advertised capabilities, and scratch space
// (CONFIGURATION.md Sections 6.1-6.3).
type WorkerConfig struct {
	// ID is the stable identifier the Worker registers under. It must be a UUID
	// (the API rejects non-UUID worker ids). When unset, a UUID is generated and
	// persisted at <scratch_dir>/worker-id on first boot and reused thereafter
	// (see resolveWorkerID).
	ID string
	// Drivers is the ExecutionDriver set this Worker advertises.
	Drivers []string
	// MaxServers is the free-capacity hint; 0 means "no advertised cap".
	MaxServers uint32
	// ScratchDir is the local working-set root.
	ScratchDir string
	// MetricsIntervalSeconds is the cadence for periodic per-server Metrics events
	// (FR-MON-3). 0 keeps the in-code default.
	MetricsIntervalSeconds uint32
}

// DriverConfig holds per-driver settings (CONFIGURATION.md Section 6.3). Only the
// container driver has configurable inputs at this milestone.
type DriverConfig struct {
	// Container configures the container (Docker) ExecutionDriver. It is consulted
	// only when worker.drivers advertises "container".
	Container ContainerConfig
}

// ContainerConfig configures the container driver (CONFIGURATION.md Section 6.3).
type ContainerConfig struct {
	// DockerHost is the Docker daemon endpoint (unix:// socket). Empty uses the
	// daemon default socket.
	DockerHost string
	// Images maps a Java major version to the base container image providing that
	// JRE. The driver selects the image for the major a server's Minecraft version
	// requires.
	Images map[int]string
	// GameBindIP is the host interface the driver publishes each server's game
	// port on. The in-code default is 127.0.0.1 (loopback-only, preserving the
	// historical behavior); set 0.0.0.0 to accept players from outside the host.
	// RCON always stays on loopback regardless of this value.
	GameBindIP string
	// Network is the user-defined Docker network the driver attaches each MC
	// container to. Empty (the default) keeps the historical behavior: containers
	// run on the default bridge and RCON is published to the host loopback. When
	// set — the shipped compose topology, where the Worker itself is a container —
	// the driver attaches MC containers to this network, drops the host RCON
	// publication, and dials RCON at the container's name on the network (the
	// network's container-name DNS resolves it). The game-port publication is
	// unchanged either way.
	Network string
}

// LogConfig is the observability surface (CONFIGURATION.md Section 6.4).
type LogConfig struct {
	Level  string
	Format string
}

// fileConfig mirrors Config with TOML tags so the file form nests each logical
// key under its group (CONFIGURATION.md Section 2). Pointers distinguish "unset"
// (keep the default) from a zero value the file explicitly supplied.
type fileConfig struct {
	API struct {
		GRPCEndpoint *string `toml:"grpc_endpoint"`
		Credential   *string `toml:"credential"`
		TLS          struct {
			CAFile         *string `toml:"ca_file"`
			Insecure       *bool   `toml:"insecure"`
			ClientCertFile *string `toml:"client_cert_file"`
			ClientKeyFile  *string `toml:"client_key_file"`
		} `toml:"tls"`
	} `toml:"api"`
	Worker struct {
		ID                     *string  `toml:"id"`
		Drivers                []string `toml:"drivers"`
		MaxServers             *uint32  `toml:"max_servers"`
		ScratchDir             *string  `toml:"scratch_dir"`
		MetricsIntervalSeconds *uint32  `toml:"metrics_interval_seconds"`
	} `toml:"worker"`
	Driver struct {
		Container struct {
			DockerHost *string `toml:"docker_host"`
			GameBindIP *string `toml:"game_bind_ip"`
			Network    *string `toml:"network"`
			// Images maps a Java major (string key, e.g. "21") to the base image
			// ref. TOML table keys are strings; they are parsed to ints.
			Images map[string]string `toml:"images"`
		} `toml:"container"`
	} `toml:"driver"`
	Log struct {
		Level  *string `toml:"level"`
		Format *string `toml:"format"`
	} `toml:"log"`
}

// defaults returns a Config holding the in-code default values
// (CONFIGURATION.md Section 6). Keys with no default stay zero and are checked
// in validate.
func defaults() Config {
	return Config{
		Worker: WorkerConfig{
			// No default driver: the only shipped driver is "container", which
			// requires driver.container.images, so there is no zero-config driver to
			// fall back to. worker.drivers is therefore effectively required and
			// validate() rejects an empty set (issue #781).
			MaxServers: 0,
		},
		Driver: DriverConfig{
			Container: ContainerConfig{
				GameBindIP: "127.0.0.1",
			},
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
// is a fatal error the caller surfaces at boot (CONFIGURATION.md Section 2).
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

	// Resolve worker.id after validate so worker.scratch_dir is guaranteed set:
	// an unset id is persisted under it (see resolveWorkerID).
	if err := resolveWorkerID(&cfg); err != nil {
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
	setString(&cfg.API.TLS.ClientCertFile, fc.API.TLS.ClientCertFile)
	setString(&cfg.API.TLS.ClientKeyFile, fc.API.TLS.ClientKeyFile)
	setString(&cfg.Worker.ID, fc.Worker.ID)
	if fc.Worker.Drivers != nil {
		cfg.Worker.Drivers = fc.Worker.Drivers
	}
	if fc.Worker.MaxServers != nil {
		cfg.Worker.MaxServers = *fc.Worker.MaxServers
	}
	if fc.Worker.MetricsIntervalSeconds != nil {
		cfg.Worker.MetricsIntervalSeconds = *fc.Worker.MetricsIntervalSeconds
	}
	setString(&cfg.Worker.ScratchDir, fc.Worker.ScratchDir)
	setString(&cfg.Driver.Container.DockerHost, fc.Driver.Container.DockerHost)
	setString(&cfg.Driver.Container.GameBindIP, fc.Driver.Container.GameBindIP)
	setString(&cfg.Driver.Container.Network, fc.Driver.Container.Network)
	if fc.Driver.Container.Images != nil {
		images, err := parseMajorMap("driver.container.images", fc.Driver.Container.Images)
		if err != nil {
			return err
		}
		cfg.Driver.Container.Images = images
	}
	setString(&cfg.Log.Level, fc.Log.Level)
	setString(&cfg.Log.Format, fc.Log.Format)

	return nil
}

// applyEnv overlays MCD_WORKER_ environment variables onto cfg (highest
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
	setEnvString(&cfg.API.TLS.ClientCertFile, getenv, "API_TLS_CLIENT_CERT_FILE")
	setEnvString(&cfg.API.TLS.ClientKeyFile, getenv, "API_TLS_CLIENT_KEY_FILE")
	setEnvString(&cfg.Worker.ID, getenv, "WORKER_ID")
	setEnvString(&cfg.Worker.ScratchDir, getenv, "WORKER_SCRATCH_DIR")
	setEnvString(&cfg.Log.Level, getenv, "LOG_LEVEL")
	setEnvString(&cfg.Log.Format, getenv, "LOG_FORMAT")

	if v := getenv(EnvPrefix + "WORKER_DRIVERS"); v != "" {
		cfg.Worker.Drivers = splitList(v)
	}
	if v := getenv(EnvPrefix + "WORKER_MAX_SERVERS"); v != "" {
		n, err := strconv.ParseUint(v, 10, 32)
		if err != nil {
			return fmt.Errorf("config: %sWORKER_MAX_SERVERS: %w", EnvPrefix, err)
		}
		cfg.Worker.MaxServers = uint32(n)
	}
	if v := getenv(EnvPrefix + "WORKER_METRICS_INTERVAL_SECONDS"); v != "" {
		n, err := strconv.ParseUint(v, 10, 32)
		if err != nil {
			return fmt.Errorf("config: %sWORKER_METRICS_INTERVAL_SECONDS: %w", EnvPrefix, err)
		}
		cfg.Worker.MetricsIntervalSeconds = uint32(n)
	}

	setEnvString(&cfg.Driver.Container.DockerHost, getenv, "DRIVER_CONTAINER_DOCKER_HOST")
	setEnvString(&cfg.Driver.Container.GameBindIP, getenv, "DRIVER_CONTAINER_GAME_BIND_IP")
	setEnvString(&cfg.Driver.Container.Network, getenv, "DRIVER_CONTAINER_NETWORK")

	// DRIVER_CONTAINER_IMAGES is a comma-separated list of major=image pairs, e.g.
	// "17=eclipse-temurin:17-jre,21=eclipse-temurin:21-jre".
	if v := getenv(EnvPrefix + "DRIVER_CONTAINER_IMAGES"); v != "" {
		images, err := parseRuntimePairs(v)
		if err != nil {
			return fmt.Errorf("config: %sDRIVER_CONTAINER_IMAGES: %w", EnvPrefix, err)
		}
		cfg.Driver.Container.Images = images
	}

	return nil
}

// parseMajorMap converts a string-keyed Java-major map (from TOML) to an
// int-keyed one, erroring on a non-integer major. key names the config key for
// the error message.
func parseMajorMap(key string, in map[string]string) (map[int]string, error) {
	out := make(map[int]string, len(in))
	for k, value := range in {
		major, err := strconv.Atoi(k)
		if err != nil {
			return nil, fmt.Errorf("config: %s: key %q is not a Java major version: %w", key, k, err)
		}
		out[major] = value
	}
	return out, nil
}

// parseRuntimePairs parses a comma-separated major=path list into a runtimes map.
func parseRuntimePairs(v string) (map[int]string, error) {
	out := map[int]string{}
	for _, pair := range strings.Split(v, ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		key, path, ok := strings.Cut(pair, "=")
		if !ok {
			return nil, fmt.Errorf("entry %q is not in major=path form", pair)
		}
		major, err := strconv.Atoi(strings.TrimSpace(key))
		if err != nil {
			return nil, fmt.Errorf("entry %q has a non-integer Java major: %w", pair, err)
		}
		out[major] = strings.TrimSpace(path)
	}
	return out, nil
}

// validate enforces the required keys with no default (CONFIGURATION.md Section
// 6) and the documented value sets. Transport security is required unless
// explicitly opted out: a CA file enables TLS, api.tls.insecure=true permits a
// plaintext dial, and neither set is a fatal error (CONFIGURATION.md Section
// 6.1).
func (c Config) validate() error {
	var missing []string
	if c.API.GRPCEndpoint == "" {
		missing = append(missing, "api.grpc_endpoint")
	}
	if c.API.Credential == "" {
		missing = append(missing, "api.credential")
	}
	if c.Worker.ScratchDir == "" {
		missing = append(missing, "worker.scratch_dir")
	}
	if len(missing) > 0 {
		return fmt.Errorf("config: missing required key(s): %s", strings.Join(missing, ", "))
	}

	if c.API.TLS.CAFile == "" && !c.API.TLS.Insecure {
		return fmt.Errorf("config: api.tls.ca_file is required (or set api.tls.insecure=true for a plaintext dev dial)")
	}

	if len(c.Worker.Drivers) == 0 {
		return fmt.Errorf("config: worker.drivers: must advertise at least one driver (want container)")
	}
	for _, d := range c.Worker.Drivers {
		if d != "container" {
			return fmt.Errorf("config: worker.drivers: unknown driver %q (want container)", d)
		}
		if d == "container" && len(c.Driver.Container.Images) == 0 {
			return fmt.Errorf("config: driver.container.images is required when worker.drivers advertises \"container\"")
		}
	}

	switch c.Log.Format {
	case "json", "text":
	default:
		return fmt.Errorf("config: log.format: unknown format %q (want json or text)", c.Log.Format)
	}

	if net.ParseIP(c.Driver.Container.GameBindIP) == nil {
		return fmt.Errorf("config: driver.container.game_bind_ip: %q is not a valid IP address", c.Driver.Container.GameBindIP)
	}

	return nil
}

func setString(dst *string, src *string) {
	if src != nil {
		*dst = *src
	}
}

func setEnvString(dst *string, getenv func(string) string, key string) {
	if v := getenv(EnvPrefix + key); v != "" {
		*dst = v
	}
}

func splitList(v string) []string {
	parts := strings.Split(v, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
