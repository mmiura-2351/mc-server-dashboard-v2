// Package ingress holds the relay's domain value types for the traffic it admits
// (docs/app/RELAY.md Sections 4, 6 and 8): the routing decision the API makes
// for an incoming connection, and the player sessions the relay reports back.
// The core packages (game, bedrock, session, relaysvc) name these types in the
// interfaces they declare; adapters/apiclient translates them to and from the
// generated relay.v1 messages.
//
// It depends on the standard library only (docs/app/ARCHITECTURE.md Section 2).
package ingress

import "time"

// Decision is the API's routing decision for one incoming connection.
type Decision int

const (
	// DecisionUnknown is an unrecognised or unspecified decision; treated as a
	// drop.
	DecisionUnknown Decision = iota
	// DecisionTunnel means the server is running and a TunnelDial was dispatched.
	DecisionTunnel
	// DecisionStopped means the slug resolves to a stopped server.
	DecisionStopped
	// DecisionNotFound means the slug does not resolve.
	DecisionNotFound
)

// Intent is what an incoming connection asks for.
type Intent int

const (
	// IntentStatus is a server-list status ping.
	IntentStatus Intent = iota
	// IntentLogin is a full login attempt.
	IntentLogin
)

// Source is the relay ingress path a session was accepted on, so the API can
// label the history row honestly (issue #1912).
type Source int

const (
	// SourceUnspecified is the zero value; the API stores it as legacy.
	SourceUnspecified Source = iota
	// SourceJava is a Java Edition login-session from the game listener.
	SourceJava
	// SourceBedrock is a Bedrock (Geyser) flow-session from the tunnel path.
	SourceBedrock
)

// ResolveResult is the outcome of a ResolveJoin call.
type ResolveResult struct {
	Decision Decision
	// Token is the single-use tunnel token, set only when Decision is Tunnel.
	Token string
	// ServerID is the API's identifier for the resolved server, set only when
	// Decision is Tunnel (carried into SessionStart.server_id).
	ServerID string
	// DisplayName is the server's name, set only when Decision is Stopped.
	DisplayName string
}

// SessionStart and SessionEnd are the session lifecycle events the relay
// batches to the API (RELAY.md Sections 6 and 8).
type SessionStart struct {
	SessionID string
	ServerID  string
	Slug      string
	PlayerIP  string
	Username  string
	PlayerUID string
	StartedAt time.Time
	Source    Source
}

// SessionEnd is the closing of a player session (RELAY.md Section 8).
type SessionEnd struct {
	SessionID string
	EndedAt   time.Time
}
