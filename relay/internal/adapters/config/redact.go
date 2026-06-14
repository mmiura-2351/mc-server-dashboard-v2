package config

import "log/slog"

// maskedSecret placeholders are logged in place of any secret value, so a secret
// is never echoed (RELAY.md Section 11, CONFIGURATION.md Section 3). The "set" /
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
			),
		),
		slog.Group("game",
			slog.String("listen", c.Game.Listen),
			slog.Uint64("status_cache_seconds", uint64(c.Game.StatusCacheSeconds)),
			slog.Uint64("status_cache_max_entries", uint64(c.Game.StatusCacheMaxEntries)),
			slog.Uint64("max_conns_per_ip", uint64(c.Game.MaxConnsPerIP)),
			slog.Uint64("joins_per_ip_per_second", uint64(c.Game.JoinsPerIPPerSecond)),
		),
		slog.Group("tunnel",
			slog.String("listen", c.Tunnel.Listen),
			slog.String("public_endpoint", c.Tunnel.PublicEndpoint),
			slog.Uint64("max_conns_per_ip", uint64(c.Tunnel.MaxConnsPerIP)),
			slog.Group("tls",
				slog.String("cert_file", c.Tunnel.TLS.CertFile),
				slog.String("key_file", masked(c.Tunnel.TLS.KeyFile)),
				slog.String("advertised_ca_file", c.Tunnel.TLS.AdvertisedCAFile),
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
