package apiclient

import (
	"context"
	"errors"
	"testing"
	"time"

	relayv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/relay/v1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// --- fromProtoDecision ---

func TestFromProtoDecisionTunnel(t *testing.T) {
	if got := fromProtoDecision(relayv1.JoinDecision_JOIN_DECISION_TUNNEL); got != DecisionTunnel {
		t.Errorf("TUNNEL → %d, want %d", got, DecisionTunnel)
	}
}

func TestFromProtoDecisionStopped(t *testing.T) {
	if got := fromProtoDecision(relayv1.JoinDecision_JOIN_DECISION_STOPPED); got != DecisionStopped {
		t.Errorf("STOPPED → %d, want %d", got, DecisionStopped)
	}
}

func TestFromProtoDecisionNotFound(t *testing.T) {
	if got := fromProtoDecision(relayv1.JoinDecision_JOIN_DECISION_NOT_FOUND); got != DecisionNotFound {
		t.Errorf("NOT_FOUND → %d, want %d", got, DecisionNotFound)
	}
}

func TestFromProtoDecisionUnspecified(t *testing.T) {
	if got := fromProtoDecision(relayv1.JoinDecision_JOIN_DECISION_UNSPECIFIED); got != DecisionUnknown {
		t.Errorf("UNSPECIFIED → %d, want %d", got, DecisionUnknown)
	}
}

func TestFromProtoDecisionUnrecognised(t *testing.T) {
	// A future enum value the relay does not know about falls to DecisionUnknown.
	if got := fromProtoDecision(relayv1.JoinDecision(99)); got != DecisionUnknown {
		t.Errorf("unknown(99) → %d, want %d", got, DecisionUnknown)
	}
}

// --- toProtoIntent ---

func TestToProtoIntentStatus(t *testing.T) {
	if got := toProtoIntent(IntentStatus); got != relayv1.JoinIntent_JOIN_INTENT_STATUS {
		t.Errorf("IntentStatus → %v, want JOIN_INTENT_STATUS", got)
	}
}

func TestToProtoIntentLogin(t *testing.T) {
	if got := toProtoIntent(IntentLogin); got != relayv1.JoinIntent_JOIN_INTENT_LOGIN {
		t.Errorf("IntentLogin → %v, want JOIN_INTENT_LOGIN", got)
	}
}

// --- ReportSessions proto construction ---

// fakeRelayServiceClient captures the last ReportSessions request for assertion.
type fakeRelayServiceClient struct {
	relayv1.RelayServiceClient
	lastReq *relayv1.ReportSessionsRequest
}

func (f *fakeRelayServiceClient) ReportSessions(_ context.Context, req *relayv1.ReportSessionsRequest, _ ...grpc.CallOption) (*relayv1.ReportSessionsResponse, error) {
	f.lastReq = req
	return &relayv1.ReportSessionsResponse{}, nil
}

func TestReportSessionsProtoConstruction(t *testing.T) {
	fake := &fakeRelayServiceClient{}
	c := &Client{rpc: fake, credential: "test-cred"}

	now := time.Date(2025, 6, 15, 12, 0, 0, 0, time.UTC)
	starts := []SessionStart{
		{
			SessionID: "s1",
			ServerID:  "srv-1",
			Slug:      "amber",
			PlayerIP:  "10.0.0.1",
			Username:  "Steve",
			PlayerUID: "uuid-1",
			StartedAt: now,
			Source:    SourceJava,
		},
	}
	ends := []SessionEnd{
		{
			SessionID: "s2",
			EndedAt:   now.Add(5 * time.Minute),
		},
	}

	if err := c.ReportSessions(context.Background(), starts, ends); err != nil {
		t.Fatalf("ReportSessions: %v", err)
	}

	events := fake.lastReq.GetEvents()
	if len(events) != 2 {
		t.Fatalf("events = %d, want 2", len(events))
	}

	// First event: SessionStart.
	s := events[0].GetStart()
	if s == nil {
		t.Fatal("events[0] is not a start event")
	}
	if s.GetSessionId() != "s1" {
		t.Errorf("session_id = %q, want s1", s.GetSessionId())
	}
	if s.GetServerId() != "srv-1" {
		t.Errorf("server_id = %q, want srv-1", s.GetServerId())
	}
	if s.GetSlug() != "amber" {
		t.Errorf("slug = %q, want amber", s.GetSlug())
	}
	if s.GetPlayerIp() != "10.0.0.1" {
		t.Errorf("player_ip = %q, want 10.0.0.1", s.GetPlayerIp())
	}
	if s.GetUsername() != "Steve" {
		t.Errorf("username = %q, want Steve", s.GetUsername())
	}
	if s.GetPlayerUuid() != "uuid-1" {
		t.Errorf("player_uuid = %q, want uuid-1", s.GetPlayerUuid())
	}
	if s.GetSource() != relayv1.SessionSource_SESSION_SOURCE_JAVA {
		t.Errorf("source = %v, want SESSION_SOURCE_JAVA", s.GetSource())
	}
	wantTS := timestamppb.New(now)
	if s.GetStartedAt().GetSeconds() != wantTS.GetSeconds() || s.GetStartedAt().GetNanos() != wantTS.GetNanos() {
		t.Errorf("started_at = %v, want %v", s.GetStartedAt(), wantTS)
	}

	// Second event: SessionEnd.
	e := events[1].GetEnd()
	if e == nil {
		t.Fatal("events[1] is not an end event")
	}
	if e.GetSessionId() != "s2" {
		t.Errorf("session_id = %q, want s2", e.GetSessionId())
	}
	wantEnd := timestamppb.New(now.Add(5 * time.Minute))
	if e.GetEndedAt().GetSeconds() != wantEnd.GetSeconds() || e.GetEndedAt().GetNanos() != wantEnd.GetNanos() {
		t.Errorf("ended_at = %v, want %v", e.GetEndedAt(), wantEnd)
	}
}

func TestReportSessionsEmptyNoOp(t *testing.T) {
	fake := &fakeRelayServiceClient{}
	c := &Client{rpc: fake, credential: "test-cred"}

	if err := c.ReportSessions(context.Background(), nil, nil); err != nil {
		t.Fatalf("ReportSessions with empty lists: %v", err)
	}
	if fake.lastReq != nil {
		t.Error("ReportSessions should not call the RPC when both lists are empty")
	}
}

// --- ValidateBedrockTunnel ---

// fakeValidateClient captures the last ValidateBedrockTunnel request and
// returns a canned response.
type fakeValidateClient struct {
	relayv1.RelayServiceClient
	lastReq *relayv1.ValidateBedrockTunnelRequest
	valid   bool
	err     error
}

func (f *fakeValidateClient) ValidateBedrockTunnel(_ context.Context, req *relayv1.ValidateBedrockTunnelRequest, _ ...grpc.CallOption) (*relayv1.ValidateBedrockTunnelResponse, error) {
	f.lastReq = req
	if f.err != nil {
		return nil, f.err
	}
	return &relayv1.ValidateBedrockTunnelResponse{Valid: f.valid}, nil
}

func TestValidateBedrockTunnelProtoConstruction(t *testing.T) {
	fake := &fakeValidateClient{valid: true}
	c := &Client{rpc: fake, credential: "test-cred"}

	valid, err := c.ValidateBedrockTunnel(context.Background(), "srv-1", 25701, "tok")
	if err != nil {
		t.Fatalf("ValidateBedrockTunnel: %v", err)
	}
	if !valid {
		t.Error("valid = false, want true")
	}
	if fake.lastReq.GetServerId() != "srv-1" {
		t.Errorf("server_id = %q, want srv-1", fake.lastReq.GetServerId())
	}
	if fake.lastReq.GetBedrockPort() != 25701 {
		t.Errorf("bedrock_port = %d, want 25701", fake.lastReq.GetBedrockPort())
	}
	if fake.lastReq.GetToken() != "tok" {
		t.Errorf("token = %q, want tok", fake.lastReq.GetToken())
	}
}

func TestValidateBedrockTunnelInvalid(t *testing.T) {
	fake := &fakeValidateClient{valid: false}
	c := &Client{rpc: fake, credential: "test-cred"}

	valid, err := c.ValidateBedrockTunnel(context.Background(), "srv-1", 25701, "wrong")
	if err != nil {
		t.Fatalf("ValidateBedrockTunnel: %v", err)
	}
	if valid {
		t.Error("valid = true, want false")
	}
}

func TestValidateBedrockTunnelRPCError(t *testing.T) {
	fake := &fakeValidateClient{err: errors.New("unavailable")}
	c := &Client{rpc: fake, credential: "test-cred"}

	if _, err := c.ValidateBedrockTunnel(context.Background(), "srv-1", 25701, "tok"); err == nil {
		t.Error("expected an error when the RPC fails")
	}
}
