// Package apiclient is the relay's gRPC adapter for the API's RelayService
// (docs/app/RELAY.md Section 6). It attaches the relay credential as call
// metadata ("authorization: Bearer <credential>"), mirroring the Worker's
// posture, and translates the generated relay.v1 messages to and from the
// relay's domain types (domain/ingress). Transport security (TLS) is built by the
// wiring layer (cmd/relay) and injected as a ready ClientConn.
package apiclient

import (
	"context"
	"fmt"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/domain/ingress"
	relayv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/relay/v1"
)

// authMetadataKey carries the relay credential. The API's RelayService reads it
// to authenticate the call (RELAY.md Section 6).
const authMetadataKey = "authorization"

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
func (c *Client) ResolveJoin(ctx context.Context, slug, playerIP string, intent ingress.Intent) (ingress.ResolveResult, error) {
	resp, err := c.rpc.ResolveJoin(c.authCtx(ctx), &relayv1.ResolveJoinRequest{
		Slug:     slug,
		PlayerIp: playerIP,
		Intent:   toProtoIntent(intent),
	})
	if err != nil {
		return ingress.ResolveResult{}, fmt.Errorf("apiclient: resolve join: %w", err)
	}
	return ingress.ResolveResult{
		Decision:    fromProtoDecision(resp.GetDecision()),
		Token:       resp.GetToken(),
		ServerID:    resp.GetServerId(),
		DisplayName: resp.GetDisplayName(),
	}, nil
}

// ReportSessions delivers a batch of session lifecycle events. Idempotent
// server-side, so retries are safe (RELAY.md Section 6).
func (c *Client) ReportSessions(ctx context.Context, starts []ingress.SessionStart, ends []ingress.SessionEnd) error {
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
				Source:     toProtoSource(s.Source),
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

// ValidateBedrockTunnel confirms a Worker's Bedrock QUIC dial-out credential
// against the API (epic #1540, issue #1545): the API is the only party that
// remembers what it minted for OpenBedrockTunnel, so the relay asks it to
// confirm (server_id, bedrock_port, token) rather than matching locally, the
// way it does for the per-player Java tunnel token. RELAY.md-analogue:
// docs/app/BEDROCK_TUNNEL.md.
func (c *Client) ValidateBedrockTunnel(ctx context.Context, serverID string, bedrockPort uint32, token string) (bool, error) {
	resp, err := c.rpc.ValidateBedrockTunnel(c.authCtx(ctx), &relayv1.ValidateBedrockTunnelRequest{
		ServerId:    serverID,
		BedrockPort: bedrockPort,
		Token:       token,
	})
	if err != nil {
		return false, fmt.Errorf("apiclient: validate bedrock tunnel: %w", err)
	}
	return resp.GetValid(), nil
}

func toProtoIntent(intent ingress.Intent) relayv1.JoinIntent {
	if intent == ingress.IntentLogin {
		return relayv1.JoinIntent_JOIN_INTENT_LOGIN
	}
	return relayv1.JoinIntent_JOIN_INTENT_STATUS
}

func toProtoSource(source ingress.Source) relayv1.SessionSource {
	switch source {
	case ingress.SourceJava:
		return relayv1.SessionSource_SESSION_SOURCE_JAVA
	case ingress.SourceBedrock:
		return relayv1.SessionSource_SESSION_SOURCE_BEDROCK
	default:
		return relayv1.SessionSource_SESSION_SOURCE_UNSPECIFIED
	}
}

func fromProtoDecision(d relayv1.JoinDecision) ingress.Decision {
	switch d {
	case relayv1.JoinDecision_JOIN_DECISION_TUNNEL:
		return ingress.DecisionTunnel
	case relayv1.JoinDecision_JOIN_DECISION_STOPPED:
		return ingress.DecisionStopped
	case relayv1.JoinDecision_JOIN_DECISION_NOT_FOUND:
		return ingress.DecisionNotFound
	default:
		return ingress.DecisionUnknown
	}
}
