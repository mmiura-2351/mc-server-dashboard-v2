// Package scratchformat holds what the transfer adapter and the instance manager
// share about the Worker's scratch: the names of its entries, which are persisted
// on disk, and the mark of a hydrate that failed before it touched the working dir.
package scratchformat

import "errors"

// GenerationMarkerFile records the authoritative generation of a local working
// set inside scratchDir/<server_id>. The marker shares that tree's lifecycle.
const GenerationMarkerFile = ".mcsd_generation"

// HydratePrefix starts the names of per-hydrate temp and superseded trees at
// the scratch root. Held-set scans skip them and cleanup sweeps reclaim them.
const HydratePrefix = ".hydrate-"

// ErrBeforeSwap marks a hydrate error from before the swap's first rename, which
// left the working dir as it was (issue #4041). A hydrate error that does not wrap
// it may come from the swap, after which the working dir can be changed or absent.
var ErrBeforeSwap = errors.New("before the swap")
