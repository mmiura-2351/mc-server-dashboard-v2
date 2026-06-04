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
		"MCD_WORKER_API_GRPC_ENDPOINT":  "api:50051",
		"MCD_WORKER_API_DATA_PLANE_URL": "https://api/data",
		"MCD_WORKER_API_CREDENTIAL":     "secret-token",
		"MCD_WORKER_API_TLS_INSECURE":   "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR": "/var/lib/worker",
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
	if len(cfg.Worker.Drivers) != 1 || cfg.Worker.Drivers[0] != "host-process" {
		t.Errorf("Worker.Drivers = %v, want default [host-process]", cfg.Worker.Drivers)
	}
	if cfg.Worker.MaxServers != 0 {
		t.Errorf("Worker.MaxServers = %d, want default 0", cfg.Worker.MaxServers)
	}
}

func TestLoadFailsFastOnMissingRequired(t *testing.T) {
	_, err := Load("", emptyEnv)
	if err == nil {
		t.Fatal("Load() with no required keys: want error, got nil")
	}
	for _, key := range []string{"api.grpc_endpoint", "api.data_plane_url", "api.credential", "worker.scratch_dir"} {
		if !contains(err.Error(), key) {
			t.Errorf("error %q does not mention missing key %q", err.Error(), key)
		}
	}
}

func TestLoadPrecedenceFileThenEnv(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "worker.toml")
	body := `
[api]
grpc_endpoint = "file-endpoint:50051"
data_plane_url = "https://file/data"
credential = "file-credential"

[api.tls]
insecure = true

[worker]
scratch_dir = "/file/scratch"
drivers = ["host-process", "container"]
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
	if cfg.API.DataPlaneURL != "https://file/data" {
		t.Errorf("DataPlaneURL = %q, want file value", cfg.API.DataPlaneURL)
	}
	if cfg.Worker.ScratchDir != "/file/scratch" {
		t.Errorf("ScratchDir = %q, want file value", cfg.Worker.ScratchDir)
	}
	if cfg.Worker.MaxServers != 4 {
		t.Errorf("MaxServers = %d, want 4", cfg.Worker.MaxServers)
	}
	if cfg.Worker.MetricsIntervalSeconds != 30 {
		t.Errorf("MetricsIntervalSeconds = %d, want 30 from file", cfg.Worker.MetricsIntervalSeconds)
	}
	if len(cfg.Worker.Drivers) != 2 {
		t.Errorf("Drivers = %v, want two entries", cfg.Worker.Drivers)
	}
	if cfg.Log.Level != "debug" {
		t.Errorf("Log.Level = %q, want file value debug", cfg.Log.Level)
	}
	if !cfg.API.TLS.Insecure {
		t.Errorf("TLS.Insecure = false, want true from file")
	}
}

func TestLoadRejectsUnknownDriver(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":  "api:50051",
		"MCD_WORKER_API_DATA_PLANE_URL": "https://api/data",
		"MCD_WORKER_API_CREDENTIAL":     "secret",
		"MCD_WORKER_API_TLS_INSECURE":   "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR": "/scratch",
		"MCD_WORKER_WORKER_DRIVERS":     "host-process,bogus",
	})

	_, err := Load("", env)
	if err == nil {
		t.Fatal("Load() with unknown driver: want error, got nil")
	}
	if !contains(err.Error(), "bogus") {
		t.Errorf("error %q does not name the bad driver", err.Error())
	}
}

func TestLoadRejectsMalformedMaxServers(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":  "api:50051",
		"MCD_WORKER_API_DATA_PLANE_URL": "https://api/data",
		"MCD_WORKER_API_CREDENTIAL":     "secret",
		"MCD_WORKER_API_TLS_INSECURE":   "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR": "/scratch",
		"MCD_WORKER_WORKER_MAX_SERVERS": "not-a-number",
	})

	if _, err := Load("", env); err == nil {
		t.Fatal("Load() with malformed max_servers: want error, got nil")
	}
}

func TestLoadRejectsMalformedMetricsInterval(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":               "api:50051",
		"MCD_WORKER_API_DATA_PLANE_URL":              "https://api/data",
		"MCD_WORKER_API_CREDENTIAL":                  "secret",
		"MCD_WORKER_API_TLS_INSECURE":                "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR":              "/scratch",
		"MCD_WORKER_WORKER_METRICS_INTERVAL_SECONDS": "not-a-number",
	})

	if _, err := Load("", env); err == nil {
		t.Fatal("Load() with malformed metrics_interval_seconds: want error, got nil")
	}
}

func TestLoadFailsFastWhenTLSNeitherCAFileNorInsecure(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":  "api:50051",
		"MCD_WORKER_API_DATA_PLANE_URL": "https://api/data",
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

func TestLoadAcceptsCAFileWithoutInsecure(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":  "api:50051",
		"MCD_WORKER_API_DATA_PLANE_URL": "https://api/data",
		"MCD_WORKER_API_CREDENTIAL":     "secret",
		"MCD_WORKER_API_TLS_CA_FILE":    "/etc/ssl/ca.pem",
		"MCD_WORKER_WORKER_SCRATCH_DIR": "/scratch",
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

func TestLoadJavaRuntimesFromFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "worker.toml")
	body := `
[api]
grpc_endpoint = "api:50051"
data_plane_url = "https://api/data"
credential = "secret"

[api.tls]
insecure = true

[worker]
scratch_dir = "/scratch"

[worker.java.runtimes]
17 = "/jvm/17/bin/java"
21 = "/jvm/21/bin/java"
`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(path, emptyEnv)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.Worker.Java.Runtimes[17] != "/jvm/17/bin/java" || cfg.Worker.Java.Runtimes[21] != "/jvm/21/bin/java" {
		t.Fatalf("Java.Runtimes = %v, want 17 and 21 entries", cfg.Worker.Java.Runtimes)
	}
}

func TestLoadJavaRuntimesFromEnv(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":    "api:50051",
		"MCD_WORKER_API_DATA_PLANE_URL":   "https://api/data",
		"MCD_WORKER_API_CREDENTIAL":       "secret",
		"MCD_WORKER_API_TLS_INSECURE":     "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR":   "/scratch",
		"MCD_WORKER_WORKER_JAVA_RUNTIMES": "8=/jvm/8/bin/java, 21=/jvm/21/bin/java",
	})

	cfg, err := Load("", env)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.Worker.Java.Runtimes[8] != "/jvm/8/bin/java" || cfg.Worker.Java.Runtimes[21] != "/jvm/21/bin/java" {
		t.Fatalf("Java.Runtimes = %v, want 8 and 21 entries", cfg.Worker.Java.Runtimes)
	}
}

func TestLoadRejectsMalformedJavaRuntimesEnv(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":    "api:50051",
		"MCD_WORKER_API_DATA_PLANE_URL":   "https://api/data",
		"MCD_WORKER_API_CREDENTIAL":       "secret",
		"MCD_WORKER_API_TLS_INSECURE":     "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR":   "/scratch",
		"MCD_WORKER_WORKER_JAVA_RUNTIMES": "notamajor=/jvm/java",
	})

	if _, err := Load("", env); err == nil {
		t.Fatal("Load() with non-integer Java major: want error, got nil")
	}
}

func TestLoadContainerImagesFromFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "worker.toml")
	body := `
[api]
grpc_endpoint = "api:50051"
data_plane_url = "https://api/data"
credential = "secret"

[api.tls]
insecure = true

[worker]
scratch_dir = "/scratch"
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
		"MCD_WORKER_API_DATA_PLANE_URL":           "https://api/data",
		"MCD_WORKER_API_CREDENTIAL":               "secret",
		"MCD_WORKER_API_TLS_INSECURE":             "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR":           "/scratch",
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

func TestLoadRejectsContainerWithoutImages(t *testing.T) {
	env := mapEnv(map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":  "api:50051",
		"MCD_WORKER_API_DATA_PLANE_URL": "https://api/data",
		"MCD_WORKER_API_CREDENTIAL":     "secret",
		"MCD_WORKER_API_TLS_INSECURE":   "true",
		"MCD_WORKER_WORKER_SCRATCH_DIR": "/scratch",
		"MCD_WORKER_WORKER_DRIVERS":     "container",
	})

	if _, err := Load("", env); err == nil {
		t.Fatal("Load() advertising container without images: want error, got nil")
	}
}

func contains(s, sub string) bool {
	return strings.Contains(s, sub)
}
