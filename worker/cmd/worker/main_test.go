package main

import (
	"context"
	"strings"
	"testing"
)

// TestRunFailsFastOnMissingConfig verifies the wiring surfaces a fatal config
// error at boot when required keys are absent (CONFIGURATION.md Section 2). The
// test clears the relevant env so Load sees nothing.
func TestRunFailsFastOnMissingConfig(t *testing.T) {
	for _, k := range []string{
		"MCD_WORKER_CONFIG",
		"MCD_WORKER_API_GRPC_ENDPOINT",
		"MCD_WORKER_API_CREDENTIAL",
		"MCD_WORKER_WORKER_SCRATCH_DIR",
	} {
		t.Setenv(k, "")
	}

	err := run(context.Background())
	if err == nil {
		t.Fatal("run() with no config returned nil, want a fatal config error")
	}
	if !strings.Contains(err.Error(), "config") {
		t.Errorf("run() error = %v, want a config error", err)
	}
}

// TestResolveRconHost pins the RCON host-resolution gate (issue #218): only a
// container-driven server consults the container driver's resolver; every other
// server (and a worker with no container driver built) dials the host loopback
// (empty host).
func TestResolveRconHost(t *testing.T) {
	containerResolver := func(serverID string) string {
		if serverID == "srv-1" {
			return "mc-srv-1"
		}
		return ""
	}
	noContainerDriver := func(string) string { return "" }

	tests := []struct {
		name              string
		driver            string
		containerRconHost func(string) string
		serverID          string
		want              string
	}{
		{
			name:              "container driver with network resolves to the container name",
			driver:            "container",
			containerRconHost: containerResolver,
			serverID:          "srv-1",
			want:              "mc-srv-1",
		},
		{
			name:              "non-container driver keeps the loopback",
			driver:            "other",
			containerRconHost: containerResolver,
			serverID:          "srv-1",
			want:              "",
		},
		{
			name:              "container driver with no container driver built keeps the loopback",
			driver:            "container",
			containerRconHost: noContainerDriver,
			serverID:          "srv-1",
			want:              "",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := resolveRconHost(tc.driver, tc.containerRconHost, tc.serverID)
			if got != tc.want {
				t.Errorf("resolveRconHost(%q, _, %q) = %q, want %q", tc.driver, tc.serverID, got, tc.want)
			}
		})
	}
}
