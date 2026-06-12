// Package apiclient is the relay's gRPC adapter for the API's RelayService
// (docs/app/RELAY.md Section 6). It attaches the relay credential as call
// metadata ("authorization: Bearer <credential>"), mirroring the Worker's
// posture, and translates the generated relay.v1 messages to and from the
// relay's transport-neutral types. Transport security (TLS) is built by the
// wiring layer (cmd/relay) and injected as a ready ClientConn.
package apiclient

import (
	"context"
	"fmt"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/timestamppb"

	relayv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/relay/v1"
)

// authMetadataKey carries the relay credential. The API's RelayService reads it
// to authenticate the call (RELAY.md Section 6).
const authMetadataKey = "authorization"

// Decision mirrors relayv1.JoinDecision in a transport-neutral form so the game
// listener does not import the generated package.
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

// Intent mirrors relayv1.JoinIntent.
type Intent int

const (
	// IntentStatus is a server-list status ping.
	IntentStatus Intent = iota
	// IntentLogin is a full login attempt.
	IntentLogin
)

// ResolveResult is the outcome of a ResolveJoin call.
type ResolveResult struct {
	Decision Decision
	// Token is the single-use tunnel token, set only when Decision is Tunnel.
	Token string
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
}

// SessionEnd is the closing of a player session (RELAY.md Section 8).
type SessionEnd struct {
	SessionID string
	EndedAt   time.Time
}

// Client wraps the generated RelayService client and attaches the relay
// credential to every call.
type Client struct {
	rpc        relayv1.RelayServiceClient
	credential string
}

// New builds a Client over an established gRPC connection.
func New(conn grpc.ClientConnInterface, credential string) *Client {
	return &Client{rpc: relayv1.NewRelayServiceClient(conn), credential: credential}
}

// authCtx returns ctx with the relay credential attached as outgoing metadata.
func (c *Client) authCtx(ctx context.Context) context.Context {
	return metadata.AppendToOutgoingContext(ctx, authMetadataKey, "Bearer "+c.credential)
}

// Register announces the relay's tunnel endpoint, CA, and active session set,
// and learns the deployment base_domain (RELAY.md Section 6).
func (c *Client) Register(ctx context.Context, tunnelEndpoint, tunnelCAPEM string, activeSessionIDs []string) (baseDomain string, err error) {
	resp, err := c.rpc.Register(c.authCtx(ctx), &relayv1.RegisterRequest{
		TunnelEndpoint:   tunnelEndpoint,
		TunnelCaPem:      tunnelCAPEM,
		ActiveSessionIds: activeSessionIDs,
	})
	if err != nil {
		return "", fmt.Errorf("apiclient: register: %w", err)
	}
	return resp.GetBaseDomain(), nil
}

// ResolveJoin asks the API for a routing decision for one incoming connection
// (RELAY.md Sections 4 and 6).
func (c *Client) ResolveJoin(ctx context.Context, slug, playerIP string, intent Intent) (ResolveResult, error) {
	resp, err := c.rpc.ResolveJoin(c.authCtx(ctx), &relayv1.ResolveJoinRequest{
		Slug:     slug,
		PlayerIp: playerIP,
		Intent:   toProtoIntent(intent),
	})
	if err != nil {
		return ResolveResult{}, fmt.Errorf("apiclient: resolve join: %w", err)
	}
	return ResolveResult{
		Decision:    fromProtoDecision(resp.GetDecision()),
		Token:       resp.GetToken(),
		DisplayName: resp.GetDisplayName(),
	}, nil
}

// ReportSessions delivers a batch of session lifecycle events. Idempotent
// server-side, so retries are safe (RELAY.md Section 6).
func (c *Client) ReportSessions(ctx context.Context, starts []SessionStart, ends []SessionEnd) error {
	events := make([]*relayv1.SessionEvent, 0, len(starts)+len(ends))
	for _, s := range starts {
		events = append(events, &relayv1.SessionEvent{
			Event: &relayv1.SessionEvent_Start{Start: &relayv1.SessionStart{
				SessionId:  s.SessionID,
				ServerId:   s.ServerID,
				Slug:       s.Slug,
				PlayerIp:   s.PlayerIP,
				Username:   s.Username,
				PlayerUuid: s.PlayerUID,
				StartedAt:  timestamppb.New(s.StartedAt),
			}},
		})
	}
	for _, e := range ends {
		events = append(events, &relayv1.SessionEvent{
			Event: &relayv1.SessionEvent_End{End: &relayv1.SessionEnd{
				SessionId: e.SessionID,
				EndedAt:   timestamppb.New(e.EndedAt),
			}},
		})
	}
	if len(events) == 0 {
		return nil
	}
	_, err := c.rpc.ReportSessions(c.authCtx(ctx), &relayv1.ReportSessionsRequest{Events: events})
	if err != nil {
		return fmt.Errorf("apiclient: report sessions: %w", err)
	}
	return nil
}

func toProtoIntent(intent Intent) relayv1.JoinIntent {
	if intent == IntentLogin {
		return relayv1.JoinIntent_JOIN_INTENT_LOGIN
	}
	return relayv1.JoinIntent_JOIN_INTENT_STATUS
}

func fromProtoDecision(d relayv1.JoinDecision) Decision {
	switch d {
	case relayv1.JoinDecision_JOIN_DECISION_TUNNEL:
		return DecisionTunnel
	case relayv1.JoinDecision_JOIN_DECISION_STOPPED:
		return DecisionStopped
	case relayv1.JoinDecision_JOIN_DECISION_NOT_FOUND:
		return DecisionNotFound
	default:
		return DecisionUnknown
	}
}
