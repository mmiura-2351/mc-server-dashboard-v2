package instancemanager

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// contractRow is one row of the cross-language contract table
// (proto/contract/command_error_contract.json, issue #204).
type contractRow struct {
	Kind         string `json:"kind"`
	Precondition string `json:"precondition"`
	Code         string `json:"code"`
}

type contractTable struct {
	Rows []contractRow `json:"rows"`
}

// contractPath locates the shared table relative to this package: four dirs up
// from worker/internal/application/instancemanager reaches the repo root, then
// proto/contract/command_error_contract.json.
func contractPath(t *testing.T) string {
	t.Helper()
	return filepath.Join("..", "..", "..", "..", "proto", "contract", "command_error_contract.json")
}

func loadContract(t *testing.T) contractTable {
	t.Helper()
	data, err := os.ReadFile(contractPath(t))
	if err != nil {
		t.Fatalf("read contract table: %v", err)
	}
	var table contractTable
	if err := json.Unmarshal(data, &table); err != nil {
		t.Fatalf("parse contract table: %v", err)
	}
	if len(table.Rows) == 0 {
		t.Fatal("contract table has no rows")
	}
	return table
}

// wantCode maps a contract 'code' string to the result expectation: a success
// when "ok", otherwise the matching CommandErrorCode.
func wantCode(t *testing.T, code string) (success bool, errCode session.CommandErrorCode) {
	t.Helper()
	if code == "ok" {
		return true, 0
	}
	codes := map[string]session.CommandErrorCode{
		"internal":           session.CommandErrorInternal,
		"server_not_found":   session.CommandErrorServerNotFound,
		"invalid_state":      session.CommandErrorInvalidState,
		"driver_unavailable": session.CommandErrorDriverUnavailable,
		"transfer_failed":    session.CommandErrorTransferFailed,
		"file_access_denied": session.CommandErrorFileAccessDenied,
		"port_conflict":      session.CommandErrorPortConflict,
		"image_missing":      session.CommandErrorImageMissing,
	}
	c, ok := codes[code]
	if !ok {
		t.Fatalf("contract table: unknown code %q", code)
	}
	return false, c
}

// driveRow builds a Manager driven into the row's precondition, then dispatches
// one command of the row's kind and returns the result. Every precondition is
// reproduced with the existing fakes so the assertion reflects what the
// instancemanager actually emits, not a hand-fed status (the #202 fix).
func driveRow(t *testing.T, row contractRow) session.CommandResult {
	t.Helper()
	const serverID = "s1"
	ctx := context.Background()

	switch row.Precondition {
	case "instance_stopped":
		m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{})
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "instance_running":
		m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{})
		if res := m.Handle(ctx, startCmd()); !res.Success {
			t.Fatalf("seed running instance: %+v", res)
		}
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "instance_absent":
		m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{})
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "orphan_pending":
		// A failed first Stop records the server as a failed-stop orphan; the row
		// command then runs against that pending orphan (issue #251/#253).
		d := &orphanDriver{stopAfter: 1000}
		m := newManager(t, d, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{})
		if res := m.Handle(ctx, startCmd()); !res.Success {
			t.Fatalf("seed orphan: start: %+v", res)
		}
		if res := m.Handle(ctx, session.Command{CommandID: "stop1", ServerID: serverID, Kind: "StopServer"}); res.Success {
			t.Fatalf("seed orphan: first stop unexpectedly succeeded: %+v", res)
		}
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "driver_unavailable":
		m := newManager(t, &fakeDriver{}, nil)
		cmd := contractCmd(t, row.Kind, serverID)
		cmd.Driver = "container" // not offered by the test worker (only host-process)
		return m.Handle(ctx, cmd)

	case "port_conflict":
		// A driver whose Start fails with the sanitized port-conflict sentinel: the
		// container driver derives it from the Docker daemon message; here the fake
		// returns the wrapped sentinel so the assertion reflects the real
		// classification path (issue #225).
		d := &fakeDriver{startErr: fmt.Errorf("start: %w", execution.ErrPortConflict)}
		m := newManager(t, d, nil)
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "image_missing":
		d := &fakeDriver{startErr: fmt.Errorf("create: %w", execution.ErrImageMissing)}
		m := newManager(t, d, nil)
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "missing_path":
		m := newManager(t, &fakeDriver{}, nil)
		cmd := contractCmd(t, row.Kind, serverID)
		cmd.Path = "does/not/exist"
		return m.Handle(ctx, cmd)

	case "unsafe_path":
		m := newManager(t, &fakeDriver{}, nil)
		cmd := contractCmd(t, row.Kind, serverID)
		cmd.Path = "../escape"
		return m.Handle(ctx, cmd)

	default:
		t.Fatalf("contract table: unknown precondition %q", row.Precondition)
		return session.CommandResult{}
	}
}

// contractCmd builds a representative command of the given kind, with the fields
// each handler needs to reach its precondition branch.
func contractCmd(t *testing.T, kind, serverID string) session.Command {
	t.Helper()
	cmd := session.Command{CommandID: "contract", ServerID: serverID, Kind: kind}
	switch kind {
	case "StartServer":
		cmd.Driver = "host-process"
		cmd.MinecraftVersion = "1.21"
	case "ServerCommand":
		cmd.Line = "list"
	case "HydrateTrigger":
		cmd.TransferURL = "https://api/working-set"
		cmd.TransferToken = "tok"
	case "SnapshotTrigger":
		cmd.TransferURL = "https://api/snapshot"
		cmd.TransferToken = "tok"
	case "ReadFile", "EditFile", "ListFiles":
		cmd.Path = "server.properties"
	}
	return cmd
}

// TestCommandErrorContract is the worker side of the #204 guard: every row in the
// shared table is reproduced against the real instancemanager and the emitted
// code must equal the table. Drift on either the code or the table fails here.
func TestCommandErrorContract(t *testing.T) {
	table := loadContract(t)
	for _, row := range table.Rows {
		row := row
		name := fmt.Sprintf("%s/%s", row.Kind, row.Precondition)
		t.Run(name, func(t *testing.T) {
			res := driveRow(t, row)
			wantSuccess, wantErr := wantCode(t, row.Code)
			if res.Success != wantSuccess {
				t.Fatalf("%s: Success = %v, want %v (result %+v)", name, res.Success, wantSuccess, res)
			}
			if !wantSuccess && res.ErrorCode != wantErr {
				t.Fatalf("%s: ErrorCode = %v, want %v (%q)", name, res.ErrorCode, wantErr, row.Code)
			}
		})
	}
}
