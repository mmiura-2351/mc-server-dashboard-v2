package instancemanager

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"sync"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// contractRow is one row of the cross-language contract table
// (proto/contract/command_error_contract.json, issue #204). The row's `api`
// column is the API side's business and is checked by the Python test; this
// side reads only what it can drive.
type contractRow struct {
	Kind         string `json:"kind"`
	Precondition string `json:"precondition"`
	Code         string `json:"code"`
	Why          string `json:"why"`
}

// contractPrecondition is one declared precondition: the name rows key on, plus
// the prose defining the Worker state it denotes.
type contractPrecondition struct {
	Name        string   `json:"name"`
	Description []string `json:"description"`
}

type contractTable struct {
	Kinds         []string               `json:"kinds"`
	Preconditions []contractPrecondition `json:"preconditions"`
	Rows          []contractRow          `json:"rows"`
}

// unaffectedCode marks a cell whose precondition determines no emission for that
// kind: the handler never consults that state (the file handlers take no instance
// check), or the state cannot arise for it at all (a StopServer never invokes
// driver.Start, so no start classification can reach it). It is a ROW, not an
// absence — the table must carry one for every (kind, precondition) cell so that a
// missing row always means "forgotten", never "not applicable" (issue #2472).
const unaffectedCode = "unaffected"

// cell identifies one (kind, precondition) of the matrix the table must cover.
type cell struct {
	kind         string
	precondition string
}

func (c cell) String() string { return c.kind + "/" + c.precondition }

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
	if len(table.Kinds) == 0 || len(table.Preconditions) == 0 || len(table.Rows) == 0 {
		t.Fatal("contract table must declare kinds, preconditions and rows")
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
		"busy":               session.CommandErrorBusy,
	}
	c, ok := codes[code]
	if !ok {
		t.Fatalf("contract table: unknown code %q", code)
	}
	return false, c
}

// newContractManager builds a Manager with every optional collaborator wired to a
// fake. A handler that finds one missing answers the unpinned INTERNAL code, which
// would make a row record the missing fake rather than the precondition under
// test, so they are all present for every row.
func newContractManager(t *testing.T, d execution.ExecutionDriver) *Manager {
	t.Helper()
	return newManager(t, d, &fakeControl{reply: "ok"}).
		WithTransfer(&fakeTransfer{}).
		WithTunnelDialer(&fakeTunnelDialer{}).
		WithBedrockTunneler(&fakeBedrockTunneler{})
}

// relaunchFailDriver starts once and fails every later Start with err. It is the
// shape a RestartServer meets: its own driver.Start is the RELAUNCH, so the
// sanitized start classifications (port_conflict / image_missing) reach it only
// after the instance it restarts has already been started successfully.
type relaunchFailDriver struct {
	mu     sync.Mutex
	starts int
	err    error
}

func (d *relaunchFailDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.starts++
	if d.starts > 1 {
		return nil, d.err
	}
	return newFakeInstance(spec.ServerID), nil
}

// driveRow builds a Manager driven into the row's precondition, then dispatches
// one command of the row's kind and returns the result. Every precondition is
// reproduced with the existing fakes so the assertion reflects what the
// instancemanager actually emits, not a hand-fed status (the #202 fix). A few
// preconditions are reached differently by different kinds (a restart's driver
// comes from the recorded start spec, and its driver.Start is the relaunch), so
// those cases branch on the row's kind rather than pretending one arrangement
// reaches them all.
func driveRow(t *testing.T, row contractRow) session.CommandResult {
	t.Helper()
	const serverID = "s1"
	ctx := context.Background()

	switch row.Precondition {
	case "instance_stopped":
		m := newContractManager(t, &fakeDriver{})
		// The id's working set is present at rest (a prior hydrate/run left it):
		// SnapshotTrigger's stopped path packs it. An absent working dir is the
		// separate working_set_absent precondition (issue #1713).
		seedScratch(t, m, serverID)
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "working_set_absent":
		// No instance and no scratch dir for the id: a stopped-id snapshot must refuse
		// rather than pack the absent dir into an empty tar (issue #1713), and every
		// running-server verb sees an id this Worker knows nothing about.
		m := newContractManager(t, &fakeDriver{})
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "instance_running":
		m := newContractManager(t, &fakeDriver{})
		startRunning(t, m)
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "running_working_set_absent":
		// A tracked RUNNING instance whose working-set claim is gone from the scratch
		// root: start it, then destroy the id's dir out of band under the live process
		// (issue #2802). The restart's RELAUNCH meets launchReserved's guard here.
		m := newContractManager(t, &fakeDriver{})
		startRunning(t, m)
		if err := os.RemoveAll(filepath.Join(m.scratchDir, serverID)); err != nil {
			t.Fatal(err)
		}
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "working_set_emptied":
		// No tracked instance, orphan or reservation, and the id's dir EXISTS at the
		// scratch root but is empty of BOTH marker and content: destroyed in place
		// (issues #2802, #2813). It is the shape both directory stats passed, and the
		// one both replacements refuse — though by different predicates, a launch
		// keying on the generation marker and a stopped-id snapshot on the working
		// set's content. This fixture is where the two agree; see the precondition's
		// description in the contract table for where they part company.
		m := newContractManager(t, &fakeDriver{})
		if err := os.MkdirAll(filepath.Join(m.scratchDir, serverID), 0o750); err != nil {
			t.Fatal(err)
		}
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "orphan_pending":
		// A failed first Stop records the server as a failed-stop orphan; the row
		// command then runs against that pending orphan (issue #251/#253).
		//
		// stopAfter is how many Stops the driver fails before confirming. StopServer
		// takes 1 so the row's own command is the RETRY that terminates the orphan —
		// the contract statement for that cell is that a stop is not refused over an
		// orphan, it IS the path that resolves it. Every other kind keeps the orphan
		// immovable, so nothing can retire it mid-row.
		stopAfter := 1000
		if row.Kind == "StopServer" {
			stopAfter = 1
		}
		m := newContractManager(t, &orphanDriver{stopAfter: stopAfter})
		startRunning(t, m)
		if res := m.Handle(ctx, session.Command{CommandID: "stop1", ServerID: serverID, Kind: "StopServer"}); res.Success {
			t.Fatalf("seed orphan: first stop unexpectedly succeeded: %+v", res)
		}
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "command_in_flight":
		// Hold a StartServer mid-driver.Start so the id is reserved but no instance is
		// tracked yet — the same state a detached stop occupies between takeStoppableReserve's
		// eviction and stop confirmation (issue #780). The row command then arrives while the
		// reservation is held and must be rejected, not treated as SERVER_NOT_FOUND.
		d := newGatedDriver()
		m := newContractManager(t, d)
		// The gated start must get PAST the working-set guard (issue #2499) to reach
		// driver.Start and hold the reservation there; the row's own command is what
		// this fixture is about.
		seedScratch(t, m, serverID)
		go func() { _ = m.Handle(ctx, startCmd()) }()
		awaitEnter(t, d.entered)
		defer close(d.release)
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "snapshot_reserve_race":
		// handleSnapshot reads the running flag OUTSIDE the reservation and reserves
		// only afterwards, so a start that registers an instance in that window turns
		// the stopped path's reserve() into the "already running" refusal. The seam
		// runs the start inside exactly that window, so the row is driven by the real
		// interleaving rather than asserted by prose (issue #2472).
		m := newContractManager(t, &fakeDriver{})
		seedScratch(t, m, serverID)
		var once sync.Once
		m.snapshotAfterRunningCheck = func(string) { once.Do(func() { startRunning(t, m) }) }
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))

	case "driver_unavailable":
		if row.Kind == "RestartServer" {
			// A restart resolves its driver from the RECORDED start spec, not from the
			// command, so the state is "the running instance's driver is no longer
			// offered by this Worker" (issue #1619).
			m := newContractManager(t, &fakeDriver{})
			startRunning(t, m)
			delete(m.drivers, "container")
			return m.Handle(ctx, contractCmd(t, row.Kind, serverID))
		}
		m := newContractManager(t, &fakeDriver{})
		cmd := contractCmd(t, row.Kind, serverID)
		cmd.Driver = "nonexistent" // not offered by the test worker (only container)
		return m.Handle(ctx, cmd)

	case "port_conflict":
		// A driver whose Start fails with the sanitized port-conflict sentinel: the
		// container driver derives it from the Docker daemon message; here the fake
		// returns the wrapped sentinel so the assertion reflects the real
		// classification path (issue #225).
		return driveStartError(t, row, fmt.Errorf("start: %w", execution.ErrPortConflict))

	case "image_missing":
		return driveStartError(t, row, fmt.Errorf("create: %w", execution.ErrImageMissing))

	case "missing_path":
		m := newContractManager(t, &fakeDriver{})
		cmd := contractCmd(t, row.Kind, serverID)
		cmd.Path = "does/not/exist"
		return m.Handle(ctx, cmd)

	case "unsafe_path":
		m := newContractManager(t, &fakeDriver{})
		cmd := contractCmd(t, row.Kind, serverID)
		cmd.Path = "../escape"
		return m.Handle(ctx, cmd)

	case "unsafe_server_id":
		// An empty server id collapses every scratch-path join onto the scratch
		// ROOT; the intake guard must reject it before any handler runs (issue
		// #782). Driven with the empty id since that is the most dangerous shape.
		m := newContractManager(t, &fakeDriver{})
		return m.Handle(ctx, contractCmd(t, row.Kind, ""))

	default:
		t.Fatalf("contract table: no fixture for precondition %q", row.Precondition)
		return session.CommandResult{}
	}
}

// driveStartError drives the row's kind against a driver whose Start fails with a
// sanitized sentinel (issue #225). StartServer meets it on its own driver.Start;
// RestartServer meets it on the relaunch, after a successful start it can restart.
func driveStartError(t *testing.T, row contractRow, startErr error) session.CommandResult {
	t.Helper()
	const serverID = "s1"
	ctx := context.Background()
	if row.Kind == "RestartServer" {
		m := newContractManager(t, &relaunchFailDriver{err: startErr})
		startRunning(t, m)
		return m.Handle(ctx, contractCmd(t, row.Kind, serverID))
	}
	m := newContractManager(t, &fakeDriver{startErr: startErr})
	// driver.Start is reachable only once the working set is present: a start over an
	// absent working dir is refused before it (issue #2499), so the sanitized start
	// classifications are preconditions ON TOP of a held working set.
	seedScratch(t, m, serverID)
	return m.Handle(ctx, contractCmd(t, row.Kind, serverID))
}

// contractCmd builds a representative command of the given kind, with the fields
// each handler needs to reach its precondition branch.
func contractCmd(t *testing.T, kind, serverID string) session.Command {
	t.Helper()
	cmd := session.Command{CommandID: "contract", ServerID: serverID, Kind: kind}
	switch kind {
	case "StartServer":
		cmd.Driver = "container"
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
	case "TunnelDial":
		cmd.TunnelEndpoint = "relay.example:25665"
		cmd.TunnelToken = "tok"
	case "OpenBedrockTunnel":
		cmd.BedrockRelayEndpoint = "relay.example:25675"
		cmd.BedrockPort = 19132
		cmd.BedrockToken = "tok"
	}
	return cmd
}

// TestContractTableCoversEveryHandledKind pins the table's kind list to the
// canonical set Manager.Handle dispatches. A new command kind must bring its own
// column of rows rather than being contracted by nobody (issue #2472).
func TestContractTableCoversEveryHandledKind(t *testing.T) {
	table := loadContract(t)
	got := slices.Clone(table.Kinds)
	want := slices.Clone(handledKinds)
	sort.Strings(got)
	sort.Strings(want)
	if !slices.Equal(got, want) {
		t.Fatalf("contract table kinds = %v, want the handled kinds %v; a kind Manager.Handle "+
			"dispatches must appear in the table (and bring a row per precondition)", got, want)
	}
}

// TestContractTableIsExhaustive is the direction the table-driven assertion below
// cannot cover on its own (issue #2472): it checks the table against the MATRIX,
// not the matrix against the table. Every (kind, precondition) cell must carry a
// row, so a precondition a handler answers but nobody wrote down is a red test
// instead of a silent gap — the shape that let {SnapshotTrigger, orphan_pending}
// and handleSnapshot's reserve race go unrecorded.
func TestContractTableIsExhaustive(t *testing.T) {
	table := loadContract(t)
	kinds := map[string]bool{}
	for _, k := range table.Kinds {
		kinds[k] = true
	}
	preconditions := map[string]bool{}
	for _, p := range table.Preconditions {
		if p.Name == "" || len(p.Description) == 0 {
			t.Errorf("contract table: precondition %+v needs a name and a description", p)
			continue
		}
		preconditions[p.Name] = true
	}

	seen := map[cell]bool{}
	for _, row := range table.Rows {
		c := cell{row.Kind, row.Precondition}
		switch {
		case !kinds[row.Kind]:
			t.Errorf("contract table: row %s names a kind the table does not declare", c)
		case !preconditions[row.Precondition]:
			t.Errorf("contract table: row %s names a precondition the table does not declare", c)
		case seen[c]:
			t.Errorf("contract table: duplicate row for %s; one cell, one row", c)
		default:
			seen[c] = true
		}
		if row.Code == unaffectedCode && row.Why == "" {
			t.Errorf("contract table: row %s is %q without a 'why'; the marker must say how we know "+
				"the precondition determines nothing for this kind", c, unaffectedCode)
		}
	}

	var missing []string
	for _, k := range table.Kinds {
		for _, p := range table.Preconditions {
			if !seen[cell{k, p.Name}] {
				missing = append(missing, cell{k, p.Name}.String())
			}
		}
	}
	if len(missing) > 0 {
		sort.Strings(missing)
		t.Errorf("contract table: %d (kind, precondition) cells have no row:\n  %v\n"+
			"Add a row for each: the code the Worker emits there, or %q with a 'why' saying "+
			"the precondition determines nothing for that kind. An absent cell must always "+
			"mean 'forgotten' (issue #2472).", len(missing), missing, unaffectedCode)
	}
}

// TestCommandErrorContract is the worker side of the #204 guard: every row in the
// shared table is reproduced against the real instancemanager and the emitted
// code must equal the table. Drift on either the code or the table fails here.
func TestCommandErrorContract(t *testing.T) {
	table := loadContract(t)
	for _, row := range table.Rows {
		if row.Code == unaffectedCode {
			continue
		}
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
