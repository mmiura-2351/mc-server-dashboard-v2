// Package scratchformat names Worker scratch entries shared by the transfer
// adapter and instance manager. These names are persisted on disk.
package scratchformat

// GenerationMarkerFile records the authoritative generation of a local working
// set inside scratchDir/<server_id>. The marker shares that tree's lifecycle.
const GenerationMarkerFile = ".mcsd_generation"

// HydratePrefix starts the names of per-hydrate temp and superseded trees at
// the scratch root. Held-set scans skip them and cleanup sweeps reclaim them.
const HydratePrefix = ".hydrate-"
