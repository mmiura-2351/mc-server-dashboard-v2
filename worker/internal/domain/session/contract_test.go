package session

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// expiredPrecondition is the contract table's precondition whose rows this
// package emits (issue #3305). Every other row is the instancemanager's, driven
// by its TestCommandErrorContract, which skips these.
const expiredPrecondition = "command_expired"

// TestExpiredContractRows drives every non-'unaffected' command_expired row of
// the cross-language contract table (proto/contract/command_error_contract.json)
// through a real lane: the row's kind waits behind a gated same-server command
// until its budget has run out, and the answer must be the row's code, emitted
// without the handler ever seeing the command. The API reads its expectations
// for the same rows from the table's api column, so the two sides cannot drift.
func TestExpiredContractRows(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "proto", "contract", "command_error_contract.json"))
	if err != nil {
		t.Fatalf("read contract table: %v", err)
	}
	var table struct {
		Rows []struct {
			Kind         string `json:"kind"`
			Precondition string `json:"precondition"`
			Code         string `json:"code"`
		} `json:"rows"`
	}
	if err := json.Unmarshal(data, &table); err != nil {
		t.Fatalf("parse contract table: %v", err)
	}

	driven := 0
	for _, row := range table.Rows {
		if row.Precondition != expiredPrecondition || row.Code == "unaffected" {
			continue
		}
		driven++
		t.Run(row.Kind, func(t *testing.T) {
			if row.Code != CommandErrorExpired.String() {
				t.Fatalf("row code = %q; the session answers an expired command %q and nothing else",
					row.Code, CommandErrorExpired)
			}
			handler := newGateHandler("RestartServer")
			transport, clock, stop := startExpiryRunner(t, handler, discardLogger())
			defer stop()

			transport.commands <- Command{CommandID: "gate", ServerID: "s1", Kind: "RestartServer"}
			transport.commands <- Command{CommandID: "row", ServerID: "s1", Kind: row.Kind, ExpiresAfter: time.Second}
			awaitReceived(t, transport)
			waitFor(t, func() bool { return handler.inflightCount() == 1 })
			advance(clock, 2*time.Second)
			close(handler.release)

			assertExpired(t, waitForResult(t, transport, "row"))
			assertNotHandled(t, handler, "row")
		})
	}
	if driven == 0 {
		t.Fatalf("the contract table has no %q row to drive", expiredPrecondition)
	}
}
