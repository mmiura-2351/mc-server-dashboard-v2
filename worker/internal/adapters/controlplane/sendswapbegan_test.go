package controlplane

import (
	"context"
	"fmt"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// A failed hydrate's word on its swap reaches the wire as it was said (issue #4041):
// true and false both present, and nothing said stays absent. The API puts back its
// awaiting_hydrate mark only on a present false, so false must not be dropped, and an
// unsaid one must not arrive as false.
func TestCommandErrorCarriesWhetherTheHydratesSwapBegan(t *testing.T) {
	yes, no := true, false
	cases := []struct {
		name string
		said *bool
	}{
		{"began", &yes},
		{"did not begin", &no},
		{"not said", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tr, stream := newCapturingTransport()

			if err := tr.SendCommandResult(context.Background(), session.CommandResult{
				CommandID: "h1", ErrorCode: session.CommandErrorTransferFailed, SwapBegan: tc.said,
			}); err != nil {
				t.Fatal(err)
			}

			got := sentCommandResult(t, stream).GetError().SwapBegan
			if fmtBool(got) != fmtBool(tc.said) {
				t.Fatalf("swap_began = %s on the wire, want %s", fmtBool(got), fmtBool(tc.said))
			}
		})
	}
}

func fmtBool(b *bool) string {
	if b == nil {
		return "absent"
	}
	return fmt.Sprint(*b)
}
