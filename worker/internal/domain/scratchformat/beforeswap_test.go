package scratchformat_test

import (
	"archive/tar"
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/datatransfer"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/regionfsck"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// The transfer adapter and the instance manager agree on how a hydrate says it failed
// before its swap (issue #4041): a pull or an unpack the real client refuses reaches the
// API as a TRANSFER_FAILED whose swap began is false.
func TestAHydrateThatFailedBeforeItsSwapSaysSo(t *testing.T) {
	var refused bytes.Buffer
	tw := tar.NewWriter(&refused)
	if err := tw.WriteHeader(&tar.Header{Name: "/abs", Mode: 0o640, Typeflag: tar.TypeReg}); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name   string
		status int
		body   []byte
	}{
		{"pull refused", http.StatusInternalServerError, nil},
		{"archive refused", http.StatusOK, refused.Bytes()},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write(tc.body)
			}))
			defer srv.Close()
			scratch := t.TempDir()
			m := instancemanager.New(nil, scratch, regionfsck.Checker{}, nil).
				WithGenerationKey(instancemanager.LoadGenerationKey(scratch, nil)).
				WithTransfer(datatransfer.New(srv.Client()))
			t.Cleanup(m.Close)

			res := m.Handle(context.Background(), session.Command{
				CommandID: "h1", ServerID: serverID, Kind: "HydrateTrigger", TransferURL: srv.URL, TransferToken: "tok",
			})

			if res.ErrorCode != session.CommandErrorTransferFailed || res.SwapBegan == nil || *res.SwapBegan {
				t.Fatalf("hydrate = %+v, want transfer-failed with SwapBegan false", res)
			}
		})
	}
}
