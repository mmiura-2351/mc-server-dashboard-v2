// Package application holds the Worker's use cases. Each use case depends only on
// the domain layer (Ports and types) and receives the Ports it needs as
// arguments; it must not import adapters, the edge, or any framework
// (see docs/app/ARCHITECTURE.md Section 2). It is empty until use cases land.
package application
