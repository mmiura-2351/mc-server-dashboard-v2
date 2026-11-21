//go:build race

package main

// raceEnabled reports whether the race detector is on. It makes sync.Pool drop
// items at random, and slog pools its buffers, so allocation counts are noise.
const raceEnabled = true
