module github.com/mmiura-2351/mc-server-dashboard-v2/relay

go 1.26

// The relay's mcsd.relay.v1 Go stubs are generated into internal/genproto by a
// dedicated buf template (proto/buf.gen.relay.yaml, run by `make proto-gen`):
// the primary template emits them under the worker module, which Go's internal/
// rule bars a sibling module from importing, so the relay keeps its own copy.
require (
	github.com/BurntSushi/toml v1.6.0
	github.com/google/uuid v1.6.0
	google.golang.org/grpc v1.81.1
	google.golang.org/protobuf v1.36.11
)

require (
	golang.org/x/net v0.51.0 // indirect
	golang.org/x/sys v0.42.0 // indirect
	golang.org/x/text v0.34.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260226221140-a57be14db171 // indirect
)
