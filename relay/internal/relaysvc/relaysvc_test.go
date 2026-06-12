package relaysvc

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
)

type fakeRegistrar struct {
	baseDomain string
	regErr     error
	regCalls   int
	lastActive []string
}

func (f *fakeRegistrar) Register(_ context.Context, _, _ string, active []string) (string, error) {
	f.regCalls++
	f.lastActive = active
	if f.regErr != nil {
		return "", f.regErr
	}
	return f.baseDomain, nil
}

func (f *fakeRegistrar) ResolveJoin(_ context.Context, _, _ string, _ apiclient.Intent) (apiclient.ResolveResult, error) {
	return apiclient.ResolveResult{Decision: apiclient.DecisionTunnel, Token: "tok"}, nil
}

type fakeSessions struct{ ids []string }

func (f fakeSessions) ActiveSessionIDs() []string { return f.ids }

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestRegisterLearnsBaseDomain(t *testing.T) {
	reg := &fakeRegistrar{baseDomain: "mc.example.com"}
	svc := New(reg, fakeSessions{ids: []string{"s1"}}, "relay:25665", "CA", discardLogger())

	if svc.BaseDomain() != "" {
		t.Error("base_domain should be empty before Register")
	}
	if err := svc.RegisterOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if svc.BaseDomain() != "mc.example.com" {
		t.Errorf("base_domain = %q after Register", svc.BaseDomain())
	}
	if len(reg.lastActive) != 1 || reg.lastActive[0] != "s1" {
		t.Errorf("Register did not carry active session ids: %v", reg.lastActive)
	}
}

func TestRegisterOnceError(t *testing.T) {
	reg := &fakeRegistrar{regErr: errors.New("down")}
	svc := New(reg, fakeSessions{}, "relay:25665", "", discardLogger())
	if err := svc.RegisterOnce(context.Background()); err == nil {
		t.Error("RegisterOnce should surface the API error")
	}
	if svc.BaseDomain() != "" {
		t.Error("a failed Register must not set base_domain")
	}
}

func TestResolveJoinProxies(t *testing.T) {
	svc := New(&fakeRegistrar{}, fakeSessions{}, "", "", discardLogger())
	res, err := svc.ResolveJoin(context.Background(), "amber", "1.2.3.4", apiclient.IntentLogin)
	if err != nil {
		t.Fatal(err)
	}
	if res.Decision != apiclient.DecisionTunnel || res.Token != "tok" {
		t.Errorf("ResolveJoin proxy returned %+v", res)
	}
}
