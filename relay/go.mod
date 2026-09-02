module github.com/mmiura-2351/mc-server-dashboard-v2/relay

go 1.27

toolchain go1.27.0

// The relay's mcsd.relay.v1 Go stubs are generated into internal/genproto by a
// dedicated buf template (proto/buf.gen.relay.yaml, run by `make proto-gen`):
// the primary template emits them under the worker module, which Go's internal/
// rule bars a sibling module from importing, so the relay keeps its own copy.
require (
	github.com/BurntSushi/toml v1.6.0
	github.com/google/uuid v1.6.0
	google.golang.org/grpc v1.83.1
	google.golang.org/protobuf v1.36.12
)

require (
	github.com/prometheus/client_golang v1.24.1
	github.com/prometheus/client_model v0.6.2
	github.com/quic-go/quic-go v0.61.0
)

require (
	github.com/beorn7/perks v1.0.1 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/kylelemons/godebug v1.1.0 // indirect
	github.com/munnerz/goautoneg v0.0.0-20191010083416-a7dc8b61c822 // indirect
	github.com/prometheus/common v0.70.1 // indirect
	github.com/prometheus/procfs v0.21.1 // indirect
	golang.org/x/crypto v0.54.0 // indirect
	golang.org/x/net v0.57.0 // indirect
	golang.org/x/sys v0.47.0 // indirect
	golang.org/x/text v0.40.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260526163538-3dc84a4a5aaa // indirect
)
