// Package instancemanager is the Worker use case that turns control-plane
// lifecycle/console commands into ExecutionDriver calls and surfaces observed
// state transitions back onto the session (CONTROL_PLANE.md Section 5/6). It
// implements session.CommandHandler. It tracks one running instance per server
// id and owns the per-server working dir under the scratch root.
//
// Working-set posture: HydrateTrigger pulls the server's working set from the
// API data plane into scratchDir/<server_id> before launch; the API issues it
// before StartServer (FR-DATA-4). A server with no published working set yet
// hydrates to an empty dir (the endpoint is 204). SnapshotTrigger pushes the
// working set back. Hydrate/snapshot are long-running and run off the session's
// serial receive loop (issue #95); the session bounds their concurrency.
package instancemanager

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// controlFunc opens an execution.ServerControl (RCON) for a running server,
// used by ServerCommand forwarding.
type controlFunc func(ctx context.Context, serverID string) (execution.ServerControl, error)

// Transfer is the data-plane Port: move a server's working set between the API's
// authoritative Storage and the local working dir (FR-DATA-3/4). The trigger
// command carries the URL + token; the bytes ride the HTTP data plane, off the
// control-plane stream (CONTROL_PLANE.md Section 5.2).
type Transfer interface {
	// Hydrate downloads the working set from url into workingDir (an empty/204
	// response leaves it empty).
	Hydrate(ctx context.Context, url, token, workingDir string) error
	// Snapshot packs workingDir and uploads it to url.
	Snapshot(ctx context.Context, url, token, workingDir string) error
}

// Manager tracks running instances and dispatches commands to their drivers.
type Manager struct {
	drivers     map[string]execution.ExecutionDriver
	scratchDir  string
	openControl controlFunc
	transfer    Transfer
	logger      *slog.Logger

	mu        sync.Mutex
	instances map[string]execution.Instance
	// startCmds remembers the StartServer command per running server so a
	// RestartServer (which carries no driver/version) can relaunch with the same
	// spec.
	startCmds map[string]session.Command

	// events is the merged status stream the session forwards. Per-instance
	// event pumps fan their events into it.
	events chan session.StatusEvent
}

// New builds a Manager. drivers maps an advertised driver name to its adapter;
// scratchDir is the working-set root (worker.scratch_dir); openControl opens RCON
// for ServerCommand forwarding.
func New(drivers map[string]execution.ExecutionDriver, scratchDir string, openControl controlFunc) *Manager {
	return &Manager{
		drivers:     drivers,
		scratchDir:  scratchDir,
		openControl: openControl,
		logger:      slog.Default(),
		instances:   map[string]execution.Instance{},
		startCmds:   map[string]session.Command{},
		events:      make(chan session.StatusEvent, 32),
	}
}

// WithLogger sets the manager's logger.
func (m *Manager) WithLogger(l *slog.Logger) *Manager {
	m.logger = l
	return m
}

// WithTransfer wires the data-plane Transfer client used by HydrateTrigger /
// SnapshotTrigger. Without it, those commands fail with a transfer error.
func (m *Manager) WithTransfer(t Transfer) *Manager {
	m.transfer = t
	return m
}

// Events streams observed state transitions for all managed servers.
func (m *Manager) Events() <-chan session.StatusEvent { return m.events }

// Handle dispatches one command (session.CommandHandler).
func (m *Manager) Handle(ctx context.Context, cmd session.Command) session.CommandResult {
	switch cmd.Kind {
	case "StartServer":
		return m.handleStart(ctx, cmd)
	case "StopServer":
		return m.handleStop(ctx, cmd, !cmd.Force)
	case "RestartServer":
		return m.handleRestart(ctx, cmd)
	case "ServerCommand":
		return m.handleServerCommand(ctx, cmd)
	case "HydrateTrigger":
		return m.handleHydrate(ctx, cmd)
	case "SnapshotTrigger":
		return m.handleSnapshot(ctx, cmd)
	case "ReadFile":
		return m.handleReadFile(cmd)
	case "EditFile":
		return m.handleEditFile(cmd)
	default:
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: unhandled command %q", cmd.Kind))
	}
}

// handleHydrate pulls the working set into the server's working dir. It is only
// valid when the instance is stopped: hydrating a running server would replace
// the live working set out from under the process. The API issues this before
// StartServer, so the not-running precondition holds on the start path.
func (m *Manager) handleHydrate(ctx context.Context, cmd session.Command) session.CommandResult {
	if m.transfer == nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			"instancemanager: no data-plane transfer client configured")
	}
	m.mu.Lock()
	_, running := m.instances[cmd.ServerID]
	m.mu.Unlock()
	if running {
		return fail(cmd.CommandID, session.CommandErrorInvalidState,
			"instancemanager: cannot hydrate a running server")
	}

	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	if err := m.transfer.Hydrate(ctx, cmd.TransferURL, cmd.TransferToken, workingDir); err != nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: hydrate: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// handleSnapshot packs the server's working dir and uploads it. For a running
// server it first flushes pending writes with a save-all over RCON (best-effort;
// a failure is logged, not fatal) so the captured copy is as fresh as possible
// (CONTROL_PLANE.md Section 6.9).
func (m *Manager) handleSnapshot(ctx context.Context, cmd session.Command) session.CommandResult {
	if m.transfer == nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			"instancemanager: no data-plane transfer client configured")
	}
	m.mu.Lock()
	_, running := m.instances[cmd.ServerID]
	m.mu.Unlock()
	if running {
		m.flushRunning(ctx, cmd.ServerID)
	}

	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	if err := m.transfer.Snapshot(ctx, cmd.TransferURL, cmd.TransferToken, workingDir); err != nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: snapshot: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// flushRunning issues a save-all over RCON to flush pending world writes before a
// snapshot of a running server. Failures are logged, not propagated: a snapshot
// of a not-quite-flushed working set is still useful and bounded by FR-DATA-5.
func (m *Manager) flushRunning(ctx context.Context, serverID string) {
	ctrl, err := m.openControl(ctx, serverID)
	if err != nil {
		m.logger.Warn("snapshot save-all: open rcon failed", "server_id", serverID, "error", err)
		return
	}
	defer func() { _ = ctrl.Close() }()
	if _, err := ctrl.Execute(ctx, "save-all flush"); err != nil {
		m.logger.Warn("snapshot save-all failed", "server_id", serverID, "error", err)
	}
}

func (m *Manager) handleStart(ctx context.Context, cmd session.Command) session.CommandResult {
	driver, ok := m.drivers[cmd.Driver]
	if !ok {
		return fail(cmd.CommandID, session.CommandErrorDriverUnavailable,
			fmt.Sprintf("instancemanager: driver %q not offered by this Worker", cmd.Driver))
	}

	m.mu.Lock()
	if _, running := m.instances[cmd.ServerID]; running {
		m.mu.Unlock()
		return fail(cmd.CommandID, session.CommandErrorInvalidState,
			"instancemanager: server already running")
	}
	m.mu.Unlock()

	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: prepare working dir: %v", err))
	}

	inst, err := driver.Start(ctx, execution.InstanceSpec{
		ServerID:         cmd.ServerID,
		WorkingDir:       workingDir,
		MinecraftVersion: cmd.MinecraftVersion,
		JarRelpath:       cmd.JarRelpath,
	})
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: start: %v", err))
	}

	m.mu.Lock()
	m.instances[cmd.ServerID] = inst
	m.startCmds[cmd.ServerID] = cmd
	m.mu.Unlock()
	go m.pump(cmd.ServerID, inst)

	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

func (m *Manager) handleStop(ctx context.Context, cmd session.Command, graceful bool) session.CommandResult {
	inst, _, ok := m.take(cmd.ServerID)
	if !ok {
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	}
	if err := inst.Stop(ctx, graceful); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: stop: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

func (m *Manager) handleRestart(ctx context.Context, cmd session.Command) session.CommandResult {
	inst, start, ok := m.take(cmd.ServerID)
	if !ok {
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	}
	if err := inst.Stop(ctx, true); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: restart stop: %v", err))
	}
	// Relaunch with the original StartServer spec; RestartServer carries no
	// driver/jar/version of its own.
	//
	// If the relaunch fails (stop succeeded, but Start does not), the server is
	// left down and already evicted from the manager. We do not attempt recovery
	// here: the API sees the coded CommandResult error plus the observed
	// stopped/crashed status event, and desired-state reconciliation (bringing the
	// server back to its intended state) is the API's job, not the Worker's.
	res := m.handleStart(ctx, start)
	// Carry the RestartServer's correlation id so the API can match the result to
	// the command it issued, not the internal StartServer command.
	res.CommandID = cmd.CommandID
	return res
}

func (m *Manager) handleServerCommand(ctx context.Context, cmd session.Command) session.CommandResult {
	m.mu.Lock()
	_, running := m.instances[cmd.ServerID]
	m.mu.Unlock()
	if !running {
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	}

	ctrl, err := m.openControl(ctx, cmd.ServerID)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: open rcon: %v", err))
	}
	defer func() { _ = ctrl.Close() }()

	out, err := ctrl.Execute(ctx, cmd.Line)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: server command: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true, Output: out}
}

// MaxFileBytes bounds a ReadFile response and an EditFile payload. File access
// rides the control plane for small, interactive files (ARCHITECTURE.md
// Section 7.2), not bulk world data — that moves on the data plane. 4 MiB matches
// the API edge cap; an oversized read or edit is refused with a coded
// FILE_ACCESS_DENIED error rather than streaming megabytes onto the stream.
const MaxFileBytes = 4 * 1024 * 1024

// handleReadFile reads a working-set-relative file and returns its bytes
// (Section 6.9, 7.2). The path is sanitized against traversal (FR-FILE-4); a
// missing file maps to SERVER_NOT_FOUND (the API turns it into a 404) and an
// oversized file to FILE_ACCESS_DENIED. It runs inline on the receive loop: a
// small file read is fast, unlike the off-loop bulk transfers.
func (m *Manager) handleReadFile(cmd session.Command) session.CommandResult {
	target, err := safeJoin(filepath.Join(m.scratchDir, cmd.ServerID), cmd.Path)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: read file: %v", err))
	}

	info, err := os.Lstat(target)
	if err != nil {
		if os.IsNotExist(err) {
			return fail(cmd.CommandID, session.CommandErrorServerNotFound,
				fmt.Sprintf("instancemanager: read file: %q not found", cmd.Path))
		}
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: read file: %v", err))
	}
	if info.Mode()&os.ModeSymlink != 0 {
		// A symlink is the classic escape vector; refuse it outright rather than
		// following it out of the working set (FR-FILE-4).
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: refusing symlink %q", cmd.Path))
	}
	if info.IsDir() {
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: %q is a directory", cmd.Path))
	}
	if info.Size() > MaxFileBytes {
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: %q exceeds the %d-byte read cap", cmd.Path, MaxFileBytes))
	}

	content, err := os.ReadFile(target)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: read file: %v", err))
	}
	// Use a non-nil empty slice so an empty file still rides the file_content arm
	// of the result oneof (the transport distinguishes nil from empty).
	if content == nil {
		content = []byte{}
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true, FileContent: content}
}

// handleEditFile writes bytes to a working-set-relative file (Section 6.9, 7.2).
// The path is sanitized against traversal and the payload is size-bounded; the
// write is atomic (temp sibling + rename) so a concurrent reader never sees a
// torn file. It runs inline on the receive loop (a small, interactive edit).
func (m *Manager) handleEditFile(cmd session.Command) session.CommandResult {
	if len(cmd.Content) > MaxFileBytes {
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: edit exceeds the %d-byte cap", MaxFileBytes))
	}

	target, err := safeJoin(filepath.Join(m.scratchDir, cmd.ServerID), cmd.Path)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: edit file: %v", err))
	}

	// Refuse to overwrite through an existing symlink (escape vector); a fresh
	// regular file or a missing path is fine.
	if info, statErr := os.Lstat(target); statErr == nil {
		if info.Mode()&os.ModeSymlink != 0 {
			return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
				fmt.Sprintf("instancemanager: refusing symlink %q", cmd.Path))
		}
		if info.IsDir() {
			return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
				fmt.Sprintf("instancemanager: %q is a directory", cmd.Path))
		}
	}

	if err := os.MkdirAll(filepath.Dir(target), 0o750); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: edit file: %v", err))
	}
	if err := atomicWrite(target, cmd.Content); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: edit file: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// safeJoin joins name under root and verifies the result stays inside root.
// Absolute paths and any ".." component are rejected outright (not clamped),
// mirroring the data-plane extractor's discipline (FR-FILE-4). The string-level
// check below does not resolve symlinks; the handlers Lstat the final component
// and refuse a symlink so an in-path link cannot redirect the access.
func safeJoin(root, name string) (string, error) {
	slashed := filepath.ToSlash(name)
	if path.IsAbs(slashed) {
		return "", fmt.Errorf("refusing absolute path %q", name)
	}
	for _, part := range strings.Split(slashed, "/") {
		if part == ".." {
			return "", fmt.Errorf("refusing path escape %q", name)
		}
	}
	joined := filepath.Join(root, filepath.FromSlash(slashed))
	if joined != root && !strings.HasPrefix(joined, root+string(os.PathSeparator)) {
		return "", fmt.Errorf("refusing path escape %q", name)
	}
	if joined == root {
		// The working-set root itself is a directory, never a readable/writable
		// file; reject "." / "" so the caller gets a coded error, not an EISDIR.
		return "", fmt.Errorf("refusing working-set root as a file path")
	}
	return joined, nil
}

// atomicWrite writes data to a temp sibling, fsyncs it, and renames it over the
// target so a concurrent reader sees either the old or the complete new content,
// never a partial write (mirrors the Storage adapter's write discipline).
func atomicWrite(target string, data []byte) error {
	tmp, err := os.CreateTemp(filepath.Dir(target), ".edit-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, target)
}

// take removes and returns the instance and its StartServer command for
// serverID, reporting whether it was present.
func (m *Manager) take(serverID string) (execution.Instance, session.Command, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	inst, ok := m.instances[serverID]
	if !ok {
		return nil, session.Command{}, false
	}
	start := m.startCmds[serverID]
	delete(m.instances, serverID)
	delete(m.startCmds, serverID)
	return inst, start, true
}

// pump forwards an instance's status events onto the merged stream, mapping the
// domain state to its wire name. It also forgets a crashed instance so the server
// id can be started again. It exits when the instance closes its event channel.
func (m *Manager) pump(serverID string, inst execution.Instance) {
	for ev := range inst.Events() {
		if ev.State == execution.StateCrashed {
			m.forgetIf(serverID, inst)
		}
		select {
		case m.events <- session.StatusEvent{ServerID: ev.ServerID, State: ev.State.String(), Detail: ev.Detail}:
		default:
			m.logger.Warn("dropped status event; sink full", "server_id", serverID, "state", ev.State.String())
		}
	}
}

// forgetIf removes serverID's instance only if it is still the given inst, so a
// crash event does not evict a freshly restarted instance.
func (m *Manager) forgetIf(serverID string, inst execution.Instance) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.instances[serverID] == inst {
		delete(m.instances, serverID)
		delete(m.startCmds, serverID)
	}
}

// fail builds a failed CommandResult.
func fail(commandID string, code session.CommandErrorCode, msg string) session.CommandResult {
	return session.CommandResult{
		CommandID:    commandID,
		Success:      false,
		ErrorCode:    code,
		ErrorMessage: msg,
	}
}

// ensure the satisfied-interface assertion stays compile-checked.
var _ session.CommandHandler = (*Manager)(nil)
