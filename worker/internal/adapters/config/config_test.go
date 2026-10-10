package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// emptyEnv is a getenv that supplies nothing, isolating tests from the ambient
// environment.
func emptyEnv(string) string { return "" }

// mapEnv builds a getenv backed by a map.
func mapEnv(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func TestLoadAppliesDefaults(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":       "api:50051",
		"MCD_WORKER_API_CREDENTIAL":          "secret-token",
		"MCD_WORKER_API_TLS_INSECURE":        "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR":      t.TempDir(),
		"MCD_WORKER_WORKER_DRIVERS":          "container",
		"MCD_WORKER_DRIVER_CONTAINER_IMAGES": "21=eclipse-temurin:21-jre",
	})

	cfg, err := Load("", env)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}

	if got := cfg.Log.Level; got != "info" {
		t.Errorf("Log.Level = %q, want default %q", got, "info")
	}
	if got := cfg.Log.Format; got != "json" {
		t.Errorf("Log.Format = %q, want default %q", got, "json")
	}
	// worker.drivers has no default: "container" is the only shipped driver and needs images, so the value must be
	// supplied. Verify it passes through.
	if len(cfg.Worker.Drivers) != 1 || cfg.Worker.Drivers[0] != "container" {
		t.Errorf("Worker.Drivers = %v, want [container]", cfg.Worker.Drivers)
	}
	if cfg.Worker.MaxServers != 0 {
		t.Errorf("Worker.MaxServers = %d, want default 0", cfg.Worker.MaxServers)
	}
	if cfg.Driver.Container.GameBindIP != "127.0.0.1" {
		t.Errorf("Driver.Container.GameBindIP = %q, want default 127.0.0.1", cfg.Driver.Container.GameBindIP)
	}
	if cfg.Driver.Container.Network != "" {
		t.Errorf("Driver.Container.Network = %q, want default empty", cfg.Driver.Container.Network)
	}
}

func TestLoadFailsFastOnMissingRequired(t *testing.T) {
	_, err := Load("", emptyEnv)
	if err == nil {
		t.Fatal("Load() with no required keys: want error, got nil")
	}
	for _, key := range []string{"api.grpc_endpoint", "api.credential", "worker.scratch_dir"} {
		if !contains(err.Error(), key) {
			t.Errorf("error %q does not mention missing key %q", err.Error(), key)
		}
	}
}

// TestLoadFailsFastOnBlankRequired pins the whitespace-only half of the secret-blank rule (CONFIGURATION.md
// Section 3): a blank required key reads as missing, not as a value, whichever layer supplied it.
func TestLoadFailsFastOnBlankRequired(t *testing.T) {
	requiredKeys := []string{"api.grpc_endpoint", "api.credential", "worker.scratch_dir"}

	t.Run("env", func(t *testing.T) {
		env := mapEnv(map[string]string{
			"MCD_WORKER_API_GRPC_ENDPOINT":  "   ",
			"MCD_WORKER_API_CREDENTIAL":     "   ",
			"MCD_WORKER_WORKER_SCRATCH_DIR": "   ",
		})
		_, err := Load("", env)
		if err == nil {
			t.Fatal("Load() with whitespace-only required keys: want error, got nil")
		}
		for _, key := range requiredKeys {
			if !contains(err.Error(), key) {
				t.Errorf("error %q does not mention blank key %q", err.Error(), key)
			}
		}
	})

	t.Run("file", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "worker.toml")
		body := `
[api]
grpc_endpoint = "   "
credential = "   "

[worker]
scratch_dir = "   "
`
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		_, err := Load(path, emptyEnv)
		if err == nil {
			t.Fatal("Load() with whitespace-only required keys in the file: want error, got nil")
		}
		for _, key := range requiredKeys {
			if !contains(err.Error(), key) {
				t.Errorf("error %q does not mention blank key %q", err.Error(), key)
			}
		}
	})
}

func TestLoadPrecedenceFileThenEnv(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "worker.toml")
	scratch := t.TempDir()
	body := `
[api]
grpc_endpoint = "file-endpoint:50051"
credential = "file-credential"

[api.tls]
insecure = true

[worker]
scratch_dir = "` + scratch + `"
drivers = ["container"]
max_servers = 4
metrics_interval_seconds = 30

[driver.container.images]
21 = "eclipse-temurin:21-jre"

[log]
level = "debug"
`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	env := mapEnv(map[string]string{
		// env overrides the file value for the endpoint and credential only.
		"MCD_WORKER_API_GRPC_ENDPOINT": "env-endpoint:50051",
		"MCD_WORKER_API_CREDENTIAL":    "env-credential",
	})

	cfg, err := Load(path, env)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}

	if cfg.API.GRPCEndpoint != "env-endpoint:50051" {
		t.Errorf("GRPCEndpoint = %q, want env override", cfg.API.GRPCEndpoint)
	}
	if cfg.API.Credential != "env-credential" {
		t.Errorf("Credential = %q, want env override", cfg.API.Credential)
	}
	if cfg.Worker.ScratchDir != scratch {
		t.Errorf("ScratchDir = %q, want file value %q", cfg.Worker.ScratchDir, scratch)
	}
	if cfg.Worker.MaxServers != 4 {
		t.Errorf("MaxServers = %d, want 4", cfg.Worker.MaxServers)
	}
	if cfg.Worker.MetricsIntervalSeconds != 30 {
		t.Errorf("MetricsIntervalSeconds = %d, want 30 from file", cfg.Worker.MetricsIntervalSeconds)
	}
	if len(cfg.Worker.Drivers) != 1 || cfg.Worker.Drivers[0] != "container" {
		t.Errorf("Drivers = %v, want [container] from file", cfg.Worker.Drivers)
	}
	if cfg.Log.Level != "debug" {
		t.Errorf("Log.Level = %q, want file value debug", cfg.Log.Level)
	}
	if !cfg.API.TLS.Insecure {
		t.Errorf("TLS.Insecure = false, want true from file")
	}
}

// TestLoadKeepsNonBlankCredentialVerbatim pins the other side of the blank rule: only a whitespace-only value is
// collapsed. A non-blank credential is not trimmed, because the API's end of this shared secret
// (control.worker_credential, _blank_to_none) keeps it verbatim too.
func TestLoadKeepsNonBlankCredentialVerbatim(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":       "api:50051",
		"MCD_WORKER_API_CREDENTIAL":          " secret ",
		"MCD_WORKER_API_TLS_INSECURE":        "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR":      t.TempDir(),
		"MCD_WORKER_WORKER_DRIVERS":          "container",
		"MCD_WORKER_DRIVER_CONTAINER_IMAGES": "21=eclipse-temurin:21-jre",
	})

	cfg, err := Load("", env)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.API.Credential != " secret " {
		t.Errorf("Credential = %q, want %q verbatim", cfg.API.Credential, " secret ")
	}
}

// Load rejects a single invalid value on an otherwise valid config, and the error
// names the offending key so the operator can find it.
func TestLoadRejectsInvalidValue(t *testing.T) {
	tests := []struct {
		name     string
		env      map[string]string // overrides on baseEnv; "" unsets a key
		wantKeys []string
	}{
		{name: "unknown driver", env: map[string]string{"MCD_WORKER_WORKER_DRIVERS": "container,bogus"}, wantKeys: []string{"bogus"}},
		// worker.drivers no longer has a zero-config default, so a config that simply omits it is rejected, the path
		// every previously-zero-config worker now hits. The error names "container" so the operator knows what to
		// advertise.
		{name: "omitted drivers", env: map[string]string{"MCD_WORKER_WORKER_DRIVERS": ""}, wantKeys: []string{"worker.drivers", "container"}},
		{name: "container without images", env: map[string]string{"MCD_WORKER_DRIVER_CONTAINER_IMAGES": ""}, wantKeys: []string{"driver.container.images"}},
		{name: "malformed max_servers", env: map[string]string{"MCD_WORKER_WORKER_MAX_SERVERS": "not-a-number"}, wantKeys: []string{"WORKER_MAX_SERVERS"}},
		{name: "malformed metrics_interval_seconds", env: map[string]string{"MCD_WORKER_WORKER_METRICS_INTERVAL_SECONDS": "not-a-number"}, wantKeys: []string{"WORKER_METRICS_INTERVAL_SECONDS"}},
		{name: "malformed game_bind_ip", env: map[string]string{"MCD_WORKER_DRIVER_CONTAINER_GAME_BIND_IP": "not-an-ip"}, wantKeys: []string{"driver.container.game_bind_ip"}},
		{name: "root container user", env: map[string]string{"MCD_WORKER_DRIVER_CONTAINER_USER": "0:0"}, wantKeys: []string{"driver.container.user", "root"}},
		{name: "named container user", env: map[string]string{"MCD_WORKER_DRIVER_CONTAINER_USER": "minecraft:minecraft"}, wantKeys: []string{"driver.container.user", "uid:gid"}},
		{name: "container user without gid", env: map[string]string{"MCD_WORKER_DRIVER_CONTAINER_USER": "1000"}, wantKeys: []string{"driver.container.user", "uid:gid"}},
		{name: "unknown log level", env: map[string]string{"MCD_WORKER_LOG_LEVEL": "trace"}, wantKeys: []string{"log.level"}},
		{name: "log level typo", env: map[string]string{"MCD_WORKER_LOG_LEVEL": "debgu"}, wantKeys: []string{"log.level"}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			env := baseEnv(t.TempDir())
			for k, v := range tc.env {
				env[k] = v
			}

			_, err := Load("", mapEnv(env))
			if err == nil {
				t.Fatal("Load(): want error, got nil")
			}
			for _, key := range tc.wantKeys {
				if !contains(err.Error(), key) {
					t.Errorf("error %q does not name %q", err.Error(), key)
				}
			}
		})
	}
}

func TestLoadFailsFastWhenTLSNeitherCAFileNorInsecure(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":  "api:50051",
		"MCD_WORKER_API_CREDENTIAL":     "secret",
		"MCD_WORKER_WORKER_SCRATCH_DIR": "/scratch",
		// Neither api.tls.ca_file nor api.tls.insecure set.
	})

	_, err := Load("", env)
	if err == nil {
		t.Fatal("Load() with no ca_file and no insecure: want error, got nil")
	}
	if !contains(err.Error(), "api.tls.ca_file") {
		t.Errorf("error %q does not mention the required api.tls.ca_file", err.Error())
	}
}

// TestLoadFailsFastWhenTLSCAFileBlankWithoutInsecure pins that a whitespace-only api.tls.ca_file counts as
// unset, so it cannot stand in for the required CA bundle.
func TestLoadFailsFastWhenTLSCAFileBlankWithoutInsecure(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":  "api:50051",
		"MCD_WORKER_API_CREDENTIAL":     "secret",
		"MCD_WORKER_API_TLS_CA_FILE":    "   ",
		"MCD_WORKER_WORKER_SCRATCH_DIR": "/scratch",
	})

	_, err := Load("", env)
	if err == nil {
		t.Fatal("Load() with a whitespace-only ca_file and no insecure: want error, got nil")
	}
	if !contains(err.Error(), "api.tls.ca_file") {
		t.Errorf("error %q does not mention the required api.tls.ca_file", err.Error())
	}
}

// TestLoadCollapsesBlankCAFileWithInsecure pins that the value the rest of the Worker reads agrees with what
// validation judged: a whitespace-only api.tls.ca_file with api.tls.insecure=true is unset, so the dial wiring
// takes the plaintext branch rather than opening a file named " ".
func TestLoadCollapsesBlankCAFileWithInsecure(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":       "api:50051",
		"MCD_WORKER_API_CREDENTIAL":          "secret",
		"MCD_WORKER_API_TLS_CA_FILE":         "   ",
		"MCD_WORKER_API_TLS_INSECURE":        "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR":      t.TempDir(),
		"MCD_WORKER_WORKER_DRIVERS":          "container",
		"MCD_WORKER_DRIVER_CONTAINER_IMAGES": "21=eclipse-temurin:21-jre",
	})

	cfg, err := Load("", env)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.API.TLS.CAFile != "" {
		t.Errorf("TLS.CAFile = %q, want a blank value collapsed to empty", cfg.API.TLS.CAFile)
	}
}

func TestLoadAcceptsCAFileWithoutInsecure(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":       "api:50051",
		"MCD_WORKER_API_CREDENTIAL":          "secret",
		"MCD_WORKER_API_TLS_CA_FILE":         "/etc/ssl/ca.pem",
		"MCD_WORKER_WORKER_SCRATCH_DIR":      t.TempDir(),
		"MCD_WORKER_WORKER_DRIVERS":          "container",
		"MCD_WORKER_DRIVER_CONTAINER_IMAGES": "21=eclipse-temurin:21-jre",
	})

	cfg, err := Load("", env)
	if err != nil {
		t.Fatalf("Load() with ca_file error = %v", err)
	}
	if cfg.API.TLS.CAFile != "/etc/ssl/ca.pem" {
		t.Errorf("TLS.CAFile = %q, want /etc/ssl/ca.pem", cfg.API.TLS.CAFile)
	}
	if cfg.API.TLS.Insecure {
		t.Error("TLS.Insecure = true, want false (default)")
	}
}

// mtlsBaseEnv returns a minimal valid worker env (TLS verified against a CA)
// without either mTLS client-pair half set, so a test can add zero, one, or both
// halves. validate() does not read the referenced files, so placeholder paths
// are fine.
func mtlsBaseEnv(t *testing.T) map[string]string {
	t.Helper()
	return map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":       "api:50051",
		"MCD_WORKER_API_CREDENTIAL":          "secret",
		"MCD_WORKER_API_TLS_CA_FILE":         "/etc/ssl/ca.pem",
		"MCD_WORKER_WORKER_SCRATCH_DIR":      t.TempDir(),
		"MCD_WORKER_WORKER_DRIVERS":          "container",
		"MCD_WORKER_DRIVER_CONTAINER_IMAGES": "21=eclipse-temurin:21-jre",
	}
}

// Require both mTLS certificate and key, naming the missing half on failure.
func TestLoadRejectsHalfMTLSClientPair(t *testing.T) {
	tests := []struct {
		name      string
		setKey    string // env key to set (the present half)
		blankKey  string // env key to set whitespace-only (the missing half), if any
		wantNamed string // the missing half the error must name
	}{
		{name: "cert without key", setKey: "MCD_WORKER_API_TLS_CLIENT_CERT_FILE", wantNamed: "api.tls.client_key_file"},
		{name: "key without cert", setKey: "MCD_WORKER_API_TLS_CLIENT_KEY_FILE", wantNamed: "api.tls.client_cert_file"},
		// A whitespace-only half counts as unset.
		{name: "cert with blank key", setKey: "MCD_WORKER_API_TLS_CLIENT_CERT_FILE", blankKey: "MCD_WORKER_API_TLS_CLIENT_KEY_FILE", wantNamed: "api.tls.client_key_file"},
		{name: "key with blank cert", setKey: "MCD_WORKER_API_TLS_CLIENT_KEY_FILE", blankKey: "MCD_WORKER_API_TLS_CLIENT_CERT_FILE", wantNamed: "api.tls.client_cert_file"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			env := mtlsBaseEnv(t)
			env[tc.setKey] = "/etc/ssl/half.pem"
			if tc.blankKey != "" {
				env[tc.blankKey] = "   "
			}

			_, err := Load("", mapEnv(env))
			if err == nil {
				t.Fatal("Load() with a half mTLS client pair: want error, got nil")
			}
			if !contains(err.Error(), tc.wantNamed) {
				t.Errorf("error %q does not name the missing %q", err.Error(), tc.wantNamed)
			}
		})
	}
}

// Accept both mTLS files or neither.
func TestLoadAcceptsWholeOrNoMTLSClientPair(t *testing.T) {
	t.Run("both halves set", func(t *testing.T) {
		env := mtlsBaseEnv(t)
		env["MCD_WORKER_API_TLS_CLIENT_CERT_FILE"] = "/etc/ssl/client.pem"
		env["MCD_WORKER_API_TLS_CLIENT_KEY_FILE"] = "/etc/ssl/client-key.pem"
		if _, err := Load("", mapEnv(env)); err != nil {
			t.Fatalf("Load() with a whole mTLS client pair: %v", err)
		}
	})

	t.Run("neither half set", func(t *testing.T) {
		if _, err := Load("", mapEnv(mtlsBaseEnv(t))); err != nil {
			t.Fatalf("Load() with no mTLS client pair: %v", err)
		}
	})

	// Both halves whitespace-only is "neither set", and the loaded values must say so too: the dial wiring loads
	// the pair whenever both are non-empty, so a surviving " " would open files named " ".
	t.Run("both halves blank", func(t *testing.T) {
		env := mtlsBaseEnv(t)
		env["MCD_WORKER_API_TLS_CLIENT_CERT_FILE"] = "   "
		env["MCD_WORKER_API_TLS_CLIENT_KEY_FILE"] = "   "
		cfg, err := Load("", mapEnv(env))
		if err != nil {
			t.Fatalf("Load() with a blank mTLS client pair: %v", err)
		}
		if cfg.API.TLS.ClientCertFile != "" || cfg.API.TLS.ClientKeyFile != "" {
			t.Errorf("client pair = (%q, %q), want blank values collapsed to empty",
				cfg.API.TLS.ClientCertFile, cfg.API.TLS.ClientKeyFile)
		}
	})
}

func TestLoadContainerImagesFromFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "worker.toml")
	body := `
[api]
grpc_endpoint = "api:50051"
credential = "secret"

[api.tls]
insecure = true

[worker]
scratch_dir = "` + t.TempDir() + `"
drivers = ["container"]

[driver.container]
docker_host = "unix:///run/docker.sock"

[driver.container.images]
17 = "eclipse-temurin:17-jre"
21 = "eclipse-temurin:21-jre"
`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(path, emptyEnv)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.Driver.Container.DockerHost != "unix:///run/docker.sock" {
		t.Fatalf("DockerHost = %q", cfg.Driver.Container.DockerHost)
	}
	if cfg.Driver.Container.Images[17] != "eclipse-temurin:17-jre" || cfg.Driver.Container.Images[21] != "eclipse-temurin:21-jre" {
		t.Fatalf("Images = %v, want 17 and 21 entries", cfg.Driver.Container.Images)
	}
}

func TestLoadContainerImagesFromEnv(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":            "api:50051",
		"MCD_WORKER_API_CREDENTIAL":               "secret",
		"MCD_WORKER_API_TLS_INSECURE":             "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR":           t.TempDir(),
		"MCD_WORKER_WORKER_DRIVERS":               "container",
		"MCD_WORKER_DRIVER_CONTAINER_IMAGES":      "21=eclipse-temurin:21-jre",
		"MCD_WORKER_DRIVER_CONTAINER_DOCKER_HOST": "unix:///run/docker.sock",
	})

	cfg, err := Load("", env)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.Driver.Container.Images[21] != "eclipse-temurin:21-jre" {
		t.Fatalf("Images = %v, want 21 entry", cfg.Driver.Container.Images)
	}
	if cfg.Driver.Container.DockerHost != "unix:///run/docker.sock" {
		t.Fatalf("DockerHost = %q", cfg.Driver.Container.DockerHost)
	}
}

// Each primitive driver.container setting loads from the config file and from its
// environment variable onto the same field.
func TestLoadContainerSettingFromFileOrEnv(t *testing.T) {
	tests := []struct {
		name   string
		envKey string
		value  string
		got    func(Config) string
	}{
		{"game_bind_ip", "MCD_WORKER_DRIVER_CONTAINER_GAME_BIND_IP", "0.0.0.0", func(c Config) string { return c.Driver.Container.GameBindIP }},
		{"network", "MCD_WORKER_DRIVER_CONTAINER_NETWORK", "mcsd", func(c Config) string { return c.Driver.Container.Network }},
		{"user", "MCD_WORKER_DRIVER_CONTAINER_USER", loadableContainerUser(), func(c Config) string { return c.Driver.Container.User.String() }},
	}
	for _, tc := range tests {
		t.Run(tc.name+" from file", func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "worker.toml")
			body := `
[api]
grpc_endpoint = "api:50051"
credential = "secret"

[api.tls]
insecure = true

[worker]
scratch_dir = "` + t.TempDir() + `"
drivers = ["container"]

[driver.container]
` + tc.name + ` = "` + tc.value + `"

[driver.container.images]
21 = "eclipse-temurin:21-jre"
`
			if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
				t.Fatal(err)
			}

			cfg, err := Load(path, emptyEnv)
			if err != nil {
				t.Fatalf("Load() error = %v", err)
			}
			if got := tc.got(cfg); got != tc.value {
				t.Fatalf("%s = %q, want %q from file", tc.name, got, tc.value)
			}
		})

		t.Run(tc.name+" from env", func(t *testing.T) {
			env := baseEnv(t.TempDir())
			env[tc.envKey] = tc.value

			cfg, err := Load("", mapEnv(env))
			if err != nil {
				t.Fatalf("Load() error = %v", err)
			}
			if got := tc.got(cfg); got != tc.value {
				t.Fatalf("%s = %q, want %q from env", tc.name, got, tc.value)
			}
		})
	}
}

func contains(s, sub string) bool {
	return strings.Contains(s, sub)
}

// loadableContainerUser is a driver.container.user value Load accepts whoever
// runs the tests: an unprivileged process may only name its own uid:gid, and root
// may name any unprivileged one.
func loadableContainerUser() string {
	if os.Geteuid() == 0 {
		return "1234:5678"
	}
	return ContainerUser{UID: os.Geteuid(), GID: os.Getegid()}.String()
}

// The uid:gid MC containers run as follows from who the Worker is: root can serve any unprivileged user and
// defaults to a fixed one, an unprivileged Worker can only run servers as itself.
func TestResolveContainerUser(t *testing.T) {
	tests := []struct {
		name       string
		configured ContainerUser
		euid, egid int
		want       ContainerUser
		wantErr    bool
	}{
		{name: "root worker, unset: the fixed unprivileged default", euid: 0, egid: 0, want: ContainerUser{UID: 25565, GID: 25565}},
		{name: "root worker, configured: the configured user", configured: ContainerUser{UID: 2000, GID: 3000}, euid: 0, egid: 0, want: ContainerUser{UID: 2000, GID: 3000}},
		{name: "unprivileged worker, unset: the worker's own user", euid: 1000, egid: 988, want: ContainerUser{UID: 1000, GID: 988}},
		{name: "unprivileged worker, configured as itself", configured: ContainerUser{UID: 1000, GID: 988}, euid: 1000, egid: 988, want: ContainerUser{UID: 1000, GID: 988}},
		{name: "unprivileged worker, configured as someone else", configured: ContainerUser{UID: 25565, GID: 25565}, euid: 1000, egid: 988, wantErr: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := resolveContainerUser(tc.configured, tc.euid, tc.egid)
			if tc.wantErr {
				if err == nil || !strings.Contains(err.Error(), "driver.container.user") {
					t.Fatalf("error = %v, want one naming driver.container.user", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("resolveContainerUser: %v", err)
			}
			if got != tc.want {
				t.Fatalf("user = %s, want %s", got, tc.want)
			}
		})
	}
}

// With driver.container.user unset, Load still resolves a concrete, non-root
// user, so the driver never falls back to the image's default (root).
func TestLoadResolvesContainerUserWhenUnset(t *testing.T) {
	cfg, err := Load("", mapEnv(baseEnv(t.TempDir())))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	want, err := resolveContainerUser(ContainerUser{}, os.Geteuid(), os.Getegid())
	if err != nil {
		t.Fatal(err)
	}
	if got := cfg.Driver.Container.User; got != want || got.UID == 0 {
		t.Fatalf("Driver.Container.User = %s, want the non-root %s", got, want)
	}
}
