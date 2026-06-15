package session_test

// Benchmark: session domain logic (issue #1122).
//
// Measures the Backoff.Delay computation (on the reconnect hot path) and the
// IsHandledKind dispatch filter (on the per-command hot path). To add a new
// session benchmark, add a Benchmark<Function> following this pattern.

import (
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

func BenchmarkBackoffDelay(b *testing.B) {
	bo := session.DefaultBackoff
	for b.Loop() {
		bo.Delay(5, 0.42)
	}
}

func BenchmarkIsHandledKind(b *testing.B) {
	kinds := []string{
		"StartServer", "StopServer", "ServerCommand",
		"HydrateTrigger", "UnknownKind",
	}
	b.ResetTimer()
	for b.Loop() {
		for _, k := range kinds {
			session.IsHandledKind(k)
		}
	}
}
