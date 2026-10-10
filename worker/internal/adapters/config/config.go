// Package config loads defaults, then TOML, then MCD_WORKER_ environment overrides.
// Logical key paths become uppercase names with dots replaced by underscores.
package config

import (
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"

	"github.com/BurntSushi/toml"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/containerdriver"
)

// EnvPrefix is the environment-variable prefix for every Worker key
// (CONFIGURATION.md Section 2).
const EnvPrefix = "MCD_WORKER_"

// Config holds settings resolved from defaults, TOML, and environment overrides by Load.
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
	// Insecure explicitly permits plaintext and is mutually exclusive with CAFile.
	Insecure bool
	// ClientCertFile is the Worker's mTLS client certificate.
	ClientCertFile string
	// ClientKeyFile is the Worker's mTLS private key. Secret: never logged.
	ClientKeyFile string
}

// WorkerConfig is identity, advertised capabilities, and scratch space
// (CONFIGURATION.md Sections 6.1-6.3).
type WorkerConfig struct {
	// ID must be a UUID; when unset, it is generated and persisted at <scratch_dir>/worker-id.
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

// DriverConfig groups settings for the advertised execution drivers.
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
	// Images maps Java majors to base images; the Minecraft version selects the required major.
	Images map[int]string
	// GameBindIP controls game-port publication; RCON publication remains loopback-only.
	GameBindIP string
	// Network enables container DNS and replaces host RCON publication with direct network access.
	// Game-port publication is unchanged.
	Network string
	// User is always non-root after Load: root Workers default to defaultContainerUser, others to their own
	// uid:gid.
	User ContainerUser
}

// ContainerUser is a numeric uid:gid. The zero value means "unset": uid 0 is
// never a valid setting.
type ContainerUser struct {
	UID int
	GID int
}

func (u ContainerUser) String() string {
	return fmt.Sprintf("%d:%d", u.UID, u.GID)
}

// defaultContainerUser is the uid:gid MC containers run as under a Worker that
// runs as root; the driver owns the value and the reasoning.
var defaultContainerUser = ContainerUser{UID: containerdriver.DefaultRunAsUID, GID: containerdriver.DefaultRunAsGID}

// LogConfig is the observability surface (CONFIGURATION.md Section 6.4).
type LogConfig struct {
	Level  string
	Format string
}

// Pointers distinguish unset TOML values from explicit zero values.
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
			User       *string `toml:"user"`
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

func defaults() Config {
	return Config{
		Worker: WorkerConfig{
			// No default driver: container requires explicitly configured Java images.
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

// Load applies defaults, optional TOML, then environment overrides and validates the result.
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

	// Treat whitespace-only required and secret values as unset before validation.
	collapseBlank(
		&cfg.API.GRPCEndpoint,
		&cfg.API.Credential,
		&cfg.API.TLS.CAFile,
		&cfg.API.TLS.ClientCertFile,
		&cfg.API.TLS.ClientKeyFile,
		&cfg.Worker.ScratchDir,
	)

	if err := cfg.validate(); err != nil {
		return Config{}, err
	}

	user, err := resolveContainerUser(cfg.Driver.Container.User, os.Geteuid(), os.Getegid())
	if err != nil {
		return Config{}, err
	}
	cfg.Driver.Container.User = user

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
	if fc.Driver.Container.User != nil {
		user, err := parseContainerUser(*fc.Driver.Container.User)
		if err != nil {
			return fmt.Errorf("config: driver.container.user: %w", err)
		}
		cfg.Driver.Container.User = user
	}
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
	if v := getenv(EnvPrefix + "DRIVER_CONTAINER_USER"); v != "" {
		user, err := parseContainerUser(v)
		if err != nil {
			return fmt.Errorf("config: driver.container.user: %sDRIVER_CONTAINER_USER: %w", EnvPrefix, err)
		}
		cfg.Driver.Container.User = user
	}

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

// Require numeric IDs for ownership handoff and reject uid 0.
func parseContainerUser(v string) (ContainerUser, error) {
	uidText, gidText, ok := strings.Cut(v, ":")
	if !ok {
		return ContainerUser{}, fmt.Errorf("%q is not in numeric uid:gid form", v)
	}
	uid, uidErr := strconv.ParseUint(uidText, 10, 31)
	gid, gidErr := strconv.ParseUint(gidText, 10, 31)
	if uidErr != nil || gidErr != nil {
		return ContainerUser{}, fmt.Errorf("%q is not in numeric uid:gid form", v)
	}
	if uid == 0 {
		return ContainerUser{}, fmt.Errorf("%q runs the server as root (uid 0); use an unprivileged uid", v)
	}
	return ContainerUser{UID: int(uid), GID: int(gid)}, nil
}

// Unprivileged Workers must share their uid:gid with the container to manage its working set.
// Root Workers may hand ownership to a configured non-root user.
func resolveContainerUser(configured ContainerUser, euid, egid int) (ContainerUser, error) {
	unset := configured == ContainerUser{}
	if euid == 0 {
		if unset {
			return defaultContainerUser, nil
		}
		return configured, nil
	}
	own := ContainerUser{UID: euid, GID: egid}
	if !unset && configured != own {
		return ContainerUser{}, fmt.Errorf(
			"config: driver.container.user: %s differs from the worker's own %s, and only a worker running as root can run servers as another user (leave the key unset)",
			configured, own)
	}
	return own, nil
}

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

	// Reject a partial mTLS pair instead of silently connecting without a client certificate.
	if c.API.TLS.ClientCertFile == "" && c.API.TLS.ClientKeyFile != "" {
		return fmt.Errorf("config: api.tls.client_cert_file is required when api.tls.client_key_file is set")
	}
	if c.API.TLS.ClientKeyFile == "" && c.API.TLS.ClientCertFile != "" {
		return fmt.Errorf("config: api.tls.client_key_file is required when api.tls.client_cert_file is set")
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

	switch c.Log.Level {
	case "debug", "info", "warn", "error":
	default:
		return fmt.Errorf("config: log.level: unknown level %q (want debug, info, warn, or error)", c.Log.Level)
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

// collapseBlank clears whitespace-only values but preserves non-blank values verbatim.
func collapseBlank(values ...*string) {
	for _, v := range values {
		if strings.TrimSpace(*v) == "" {
			*v = ""
		}
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
