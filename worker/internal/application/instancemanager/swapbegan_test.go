package instancemanager

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// A hydrate whose transfer failed says whether its swap began (issue #4041): false
// only when the transfer marked its error as from before the swap, and true for any
// other error, so a transfer that does not say reads as one that may have replaced the
// working dir. The API puts back its awaiting_hydrate mark only on false.
func TestHydrateTransferFailureSaysWhetherItsSwapBegan(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"marked before the swap", fmt.Errorf("datatransfer: unpack: refused (%w)", scratchformat.ErrBeforeSwap), false},
		{"not marked", errors.New("datatransfer: unpack: rename: input/output error"), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{err: tc.err})

			res := m.Handle(context.Background(), hydrateCmd())

			if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
				t.Fatalf("hydrate = %+v, want transfer-failed", res)
			}
			if res.SwapBegan == nil || *res.SwapBegan != tc.want {
				t.Fatalf("SwapBegan = %v, want %v", fmtSwapBegan(res.SwapBegan), tc.want)
			}
		})
	}
}

// A Worker with no data-plane client fails the hydrate before any transfer, so the
// working dir is as it was.
func TestHydrateWithoutTransferClientSaysItsSwapDidNotBegin(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil) // no WithTransfer

	res := m.Handle(context.Background(), hydrateCmd())

	if res.ErrorCode != session.CommandErrorTransferFailed || res.SwapBegan == nil || *res.SwapBegan {
		t.Fatalf("hydrate = %+v (SwapBegan %v), want transfer-failed with SwapBegan false",
			res, fmtSwapBegan(res.SwapBegan))
	}
}

// A hydrate refused before its transfer says nothing of a swap: its code already
// says it ran no transfer, and SwapBegan is set on a hydrate's TRANSFER_FAILED only.
func TestHydrateRefusalSaysNothingOfTheSwap(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{})
	seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start failed: %+v", res)
	}

	res := m.Handle(context.Background(), hydrateCmd())

	if res.ErrorCode != session.CommandErrorInvalidState || res.SwapBegan != nil {
		t.Fatalf("hydrate over a running instance = %+v (SwapBegan %v), want invalid-state saying nothing",
			res, fmtSwapBegan(res.SwapBegan))
	}
}

func fmtSwapBegan(b *bool) string {
	if b == nil {
		return "nil"
	}
	return fmt.Sprint(*b)
}
