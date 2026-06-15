package controlplane_test

// Benchmark: proto message marshal/unmarshal (issue #1122).
//
// Measures the serialization cost of the control-plane Register message, which
// is on the hot path of every (re-)connection. To add a new proto benchmark,
// add a Benchmark<Message> function following this pattern.

import (
	"testing"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"google.golang.org/protobuf/proto"
)

func buildRegister() *controlplanev1.Register {
	return &controlplanev1.Register{
		WorkerId:      "worker-bench-001",
		WorkerVersion: "v2.0.0-bench",
		Capabilities: &controlplanev1.WorkerCapabilities{
			Drivers:    []controlplanev1.ExecutionDriverKind{controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_CONTAINER},
			MaxServers: 10,
		},
		HeldServers: []*controlplanev1.HeldServer{
			{ServerId: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", Generation: 42},
			{ServerId: "11111111-2222-3333-4444-555555555555", Generation: 7},
		},
	}
}

func BenchmarkRegisterMarshal(b *testing.B) {
	msg := buildRegister()
	b.ResetTimer()
	for b.Loop() {
		if _, err := proto.Marshal(msg); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkRegisterUnmarshal(b *testing.B) {
	msg := buildRegister()
	data, err := proto.Marshal(msg)
	if err != nil {
		b.Fatal(err)
	}
	b.ResetTimer()
	for b.Loop() {
		var out controlplanev1.Register
		if err := proto.Unmarshal(data, &out); err != nil {
			b.Fatal(err)
		}
	}
}
