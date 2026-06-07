package config

import "log/slog"

// maskedSecret is the placeholder logged in place of any secret value, so a
// secret is never echoed (CONFIGURATION.md Section 3, NFR-OBS-1). The "set" /
// "unset" distinction stays useful for diagnosing missing configuration without
// revealing the value.
const (
	secretSet   = "***set***"
	secretUnset = "***unset***"
)

// LogValue implements slog.LogValuer so a Config can be logged structurally with
// every secret masked. Use it instead of logging the Config directly.
func (c Config) LogValue() slog.Value {
	return slog.GroupValue(
		slog.Group("api",
			slog.String("grpc_endpoint", c.API.GRPCEndpoint),
			slog.String("credential", masked(c.API.Credential)),
			slog.Group("tls",
				slog.String("ca_file", c.API.TLS.CAFile),
				slog.Bool("insecure", c.API.TLS.Insecure),
				slog.String("client_cert_file", c.API.TLS.ClientCertFile),
				slog.String("client_key_file", masked(c.API.TLS.ClientKeyFile)),
			),
		),
		slog.Group("worker",
			slog.String("id", c.Worker.ID),
			slog.Any("drivers", c.Worker.Drivers),
			slog.Uint64("max_servers", uint64(c.Worker.MaxServers)),
			slog.String("scratch_dir", c.Worker.ScratchDir),
			slog.Any("java_runtimes", c.Worker.Java.Runtimes),
		),
		slog.Group("driver",
			slog.Group("container",
				slog.String("docker_host", c.Driver.Container.DockerHost),
				slog.Any("images", c.Driver.Container.Images),
				slog.String("game_bind_ip", c.Driver.Container.GameBindIP),
				slog.String("network", c.Driver.Container.Network),
			),
		),
		slog.Group("log",
			slog.String("level", c.Log.Level),
			slog.String("format", c.Log.Format),
		),
	)
}

func masked(secret string) string {
	if secret == "" {
		return secretUnset
	}
	return secretSet
}
