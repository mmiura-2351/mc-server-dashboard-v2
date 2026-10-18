// Package regioncheck holds the Worker's region-check Port and its verdict types:
// the structural check of a working set's .mca region files that runs before a
// snapshot is packed and on the held sets the boot scan advertises (issues #741,
// #834). The check itself is an adapter (adapters/regionfsck); the application
// receives it as a Checker and reads only the Report it returns.
//
// It depends on the standard library only (docs/app/ARCHITECTURE.md Section 2).
package regioncheck

// Checker structurally validates every .mca region file under a working set.
//
// Run it only against a quiesced working set: on a live world the read races the
// server's write and a healthy region false-positives as corrupt. Corruption is a
// normal return value, reported in the Report; an error is reserved for a real I/O
// failure while walking or reading. An absent root is an empty, healthy Report.
type Checker interface {
	CheckWorkingSet(root string) (Report, error)
}

// Reason is a machine-readable reason a region file failed the structural check.
type Reason int

const (
	// ReasonNone marks a structurally sound region file.
	ReasonNone Reason = iota
	// ReasonNotAligned marks a non-zero file shorter than the two header sectors
	// (a torn save). The reason name is retained from when it also covered a
	// non-4096-aligned size; that case is no longer corruption (issue #927).
	ReasonNotAligned
	// ReasonSectorOutOfBounds marks a present chunk whose sector offset/count sits
	// inside a header sector or reaches past EOF.
	ReasonSectorOutOfBounds
	// ReasonBadCompression marks a present chunk with an unknown compression scheme.
	ReasonBadCompression
	// ReasonTruncatedChunk marks a present chunk whose 5-byte prefix is missing or
	// whose declared length is empty or overruns its reserved sectors.
	ReasonTruncatedChunk
)

// String renders the reason as a stable name (matching the Python validator's
// reason codes) for log/error messages.
func (r Reason) String() string {
	switch r {
	case ReasonNone:
		return "none"
	case ReasonNotAligned:
		return "not_4096_aligned"
	case ReasonSectorOutOfBounds:
		return "sector_out_of_bounds"
	case ReasonBadCompression:
		return "bad_compression"
	case ReasonTruncatedChunk:
		return "truncated_chunk"
	default:
		return "unknown"
	}
}

// Finding is one corrupt region file and the first structural reason it failed.
type Finding struct {
	Path   string
	Reason Reason
}

// Report is the aggregate result of walking a working set's .mca files. Scanned
// counts every region file examined; Corrupt lists one finding per corrupt file
// (the first failing reason). Skipped counts the entries that were listed in
// their directory but gone, or no longer of the kind they were seen as, by the
// time the walk opened them, and so were neither descended into nor examined; it
// is zero on a working set at rest, and the caller logs it.
type Report struct {
	Scanned int
	Corrupt []Finding
	Skipped int
}

// Healthy reports whether nothing was flagged.
func (r Report) Healthy() bool { return len(r.Corrupt) == 0 }
