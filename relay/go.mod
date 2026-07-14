module github.com/mmiura-2351/mc-server-dashboard-v2/relay

go 1.26

toolchain go1.26.5

// The relay's mcsd.relay.v1 Go stubs are generated into internal/genproto by a
// dedicated buf template (proto/buf.gen.relay.yaml, run by `make proto-gen`):
// the primary template emits them under the worker module, which Go's internal/
// rule bars a sibling module from importing, so the relay keeps its own copy.
require (
	github.com/BurntSushi/toml v1.6.0
	github.com/google/uuid v1.6.0
	google.golang.org/grpc v1.82.0
	google.golang.org/protobuf v1.36.11
)

require (
	github.com/prometheus/client_golang v1.23.2
	github.com/prometheus/client_model v0.6.2
	github.com/quic-go/quic-go v0.60.0
)

require (
	github.com/beorn7/perks v1.0.1 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/kr/text v0.2.0 // indirect
	github.com/kylelemons/godebug v1.1.0 // indirect
	github.com/munnerz/goautoneg v0.0.0-20191010083416-a7dc8b61c822 // indirect
	github.com/prometheus/common v0.66.1 // indirect
	github.com/prometheus/procfs v0.16.1 // indirect
	go.yaml.in/yaml/v2 v2.4.2 // indirect
	golang.org/x/crypto v0.51.0 // indirect
	golang.org/x/net v0.55.0 // indirect
	golang.org/x/sys v0.45.0 // indirect
	golang.org/x/text v0.37.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260414002931-afd174a4e478 // indirect
)
