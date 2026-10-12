package controlplane_test

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/controlplane"
	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// One Worker's Session stream carries every server on that Worker, so a message
// gRPC refuses for its size must cost one command, never the stream (issue
// #3360). These tests run the real Runner and adapter over a real gRPC stack
// (bufconn) against an in-process API holding the same limit the real one sets.

// limits is the cross-language contract both sides' constants are pinned to.
type limits struct {
	MaxFileBytes    int `json:"max_file_bytes"`
	MaxMessageBytes int `json:"max_message_bytes"`
}

func readLimits(t *testing.T) limits {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "proto", "contract", "control_plane_limits.json"))
	if err != nil {
		t.Fatalf("read the limits contract: %v", err)
	}
	var l limits
	if err := json.Unmarshal(data, &l); err != nil {
		t.Fatalf("parse the limits contract: %v", err)
	}
	return l
}

func TestTheMessageLimitIsTheOneTheAPIHolds(t *testing.T) {
	if want := readLimits(t).MaxMessageBytes; controlplane.MaxMessageBytes != want {
		t.Fatalf("MaxMessageBytes = %d, the contract says %d", controlplane.MaxMessageBytes, want)
	}
}

// apiServer is an in-process API: it acks a Register, pushes the commands a test
// hands it, and records what the Worker sends back.
type apiServer struct {
	controlplanev1.UnimplementedWorkerServiceServer

	commands  chan *controlplanev1.ApiCommand
	results   chan *controlplanev1.WorkerMessage
	events    chan *controlplanev1.WorkerMessage
	registers atomic.Int32
}

func newAPIServer() *apiServer {
	return &apiServer{
		commands: make(chan *controlplanev1.ApiCommand),
		results:  make(chan *controlplanev1.WorkerMessage, 16),
		events:   make(chan *controlplanev1.WorkerMessage, 16),
	}
}

func (s *apiServer) Session(stream controlplanev1.WorkerService_SessionServer) error {
	if _, err := stream.Recv(); err != nil {
		return err
	}
	s.registers.Add(1)
	if err := stream.Send(acceptAck(time.Hour)); err != nil {
		return err
	}
	go func() {
		for {
			select {
			case cmd := <-s.commands:
				_ = stream.Send(&controlplanev1.ApiMessage{
					CorrelationId: cmd.GetCommandId(),
					Payload:       &controlplanev1.ApiMessage_ApiCommand{ApiCommand: cmd},
				})
			case <-stream.Context().Done():
				return
			}
		}
	}()
	for {
		msg, err := stream.Recv()
		if err != nil {
			return err
		}
		switch {
		case msg.GetCommandResult() != nil:
			s.results <- msg
		case msg.GetEvent().GetStatusChange() != nil, msg.GetEvent().GetLogLine() != nil:
			s.events <- msg
		}
	}
}

// start serves the API over bufconn with the given server options and returns a
// client connection dialed with the given options.
func (s *apiServer) start(t *testing.T, serverOpts []grpc.ServerOption, dialOpts ...grpc.DialOption) *grpc.ClientConn {
	t.Helper()
	lis := bufconn.Listen(1024 * 1024)
	gs := grpc.NewServer(serverOpts...)
	controlplanev1.RegisterWorkerServiceServer(gs, s)
	go func() { _ = gs.Serve(lis) }()

	opts := append([]grpc.DialOption{
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	}, dialOpts...)
	conn, err := grpc.NewClient("passthrough:///bufnet", opts...)
	if err != nil {
		t.Fatalf("grpc.NewClient: %v", err)
	}
	t.Cleanup(func() {
		_ = conn.Close()
		gs.Stop()
	})
	return conn
}

// limitedAPI is the server options of an API holding the shared limit in both
// directions, as the real one does (api fleet/adapters/grpc_server.py).
func limitedAPI() []grpc.ServerOption {
	return []grpc.ServerOption{
		grpc.MaxRecvMsgSize(controlplane.MaxMessageBytes),
		grpc.MaxSendMsgSize(controlplane.MaxMessageBytes),
	}
}

// scriptedHandler answers each command from a test-supplied function and exposes
// the event channels a test feeds.
type scriptedHandler struct {
	handle  func(session.Command) session.CommandResult
	events  chan session.StatusEvent
	logs    chan session.LogEvent
	metrics chan session.MetricsEvent
}

func newScriptedHandler(handle func(session.Command) session.CommandResult) *scriptedHandler {
	return &scriptedHandler{
		handle:  handle,
		events:  make(chan session.StatusEvent),
		logs:    make(chan session.LogEvent),
		metrics: make(chan session.MetricsEvent),
	}
}

func (h *scriptedHandler) Handle(_ context.Context, cmd session.Command) session.CommandResult {
	if cmd.Kind == "ServerCommand" && cmd.Line == "ping" {
		return session.CommandResult{CommandID: cmd.CommandID, Success: true, Output: "pong"}
	}
	result := h.handle(cmd)
	result.CommandID = cmd.CommandID
	return result
}
func (h *scriptedHandler) Events() <-chan session.StatusEvent   { return h.events }
func (h *scriptedHandler) Logs() <-chan session.LogEvent        { return h.logs }
func (h *scriptedHandler) Metrics() <-chan session.MetricsEvent { return h.metrics }

// lockedBuffer is a log sink a test can read while the Runner writes to it.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// runWorker runs the real Runner and adapter against api, dialed exactly as
// cmd/worker dials (controlplane.MessageSizeDialOption), and returns the
// Worker's log.
func runWorker(t *testing.T, api *apiServer, handler session.CommandHandler) *lockedBuffer {
	t.Helper()
	conn := api.start(t, limitedAPI(), controlplane.MessageSizeDialOption())
	logs := &lockedBuffer{}
	runner := session.NewRunner(
		controlplane.NewDialer(conn, "the-secret", realClock{}), testCaps(), realClock{},
		slog.New(slog.NewTextHandler(logs, nil)),
		session.WithCommandHandler(handler),
	)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = runner.Run(ctx); close(done) }()
	t.Cleanup(func() { cancel(); <-done })
	return logs
}

// roundTrip pushes cmd to the Worker and returns its CommandResult.
func (s *apiServer) roundTrip(t *testing.T, cmd *controlplanev1.ApiCommand) *controlplanev1.CommandResult {
	t.Helper()
	select {
	case s.commands <- cmd:
	case <-time.After(10 * time.Second):
		t.Fatalf("command %s was not accepted by the stream (session down?)", cmd.GetCommandId())
	}
	select {
	case msg := <-s.results:
		if msg.GetCorrelationId() != cmd.GetCommandId() {
			t.Fatalf("result for %q, want %q", msg.GetCorrelationId(), cmd.GetCommandId())
		}
		return msg.GetCommandResult()
	case <-time.After(10 * time.Second):
		t.Fatalf("no result for command %s (session down?)", cmd.GetCommandId())
		return nil
	}
}

// assertSessionSurvived proves the stream is the one the Worker registered on: a
// command for ANOTHER server round-trips and the Worker never re-registered.
func (s *apiServer) assertSessionSurvived(t *testing.T) {
	t.Helper()
	result := s.roundTrip(t, &controlplanev1.ApiCommand{
		CommandId: "ping", ServerId: "another-server",
		Command: &controlplanev1.ApiCommand_ServerCommand{ServerCommand: &controlplanev1.ServerCommand{Line: "ping"}},
	})
	if !result.GetSuccess() || result.GetCommandOutput() != "pong" {
		t.Fatalf("the follow-up command on another server failed: %v", result.GetError())
	}
	if got := s.registers.Load(); got != 1 {
		t.Fatalf("the Worker registered %d times, want 1 (the session dropped)", got)
	}
}

func readFile(path string) *controlplanev1.ApiCommand {
	return &controlplanev1.ApiCommand{
		CommandId: "cmd-1", ServerId: "server-a",
		Command: &controlplanev1.ApiCommand_ReadFile{ReadFile: &controlplanev1.ReadFile{Path: path}},
	}
}

func assertPayloadTooLarge(t *testing.T, result *controlplanev1.CommandResult) {
	t.Helper()
	if result.GetSuccess() {
		t.Fatal("the command succeeded, want it refused as too large")
	}
	if got := result.GetError().GetCode(); got != controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_FILE_ACCESS_DENIED {
		t.Errorf("error code = %v, want FILE_ACCESS_DENIED", got)
	}
	if got := result.GetError().GetFileAccessReason(); got != controlplanev1.FileAccessReason_FILE_ACCESS_REASON_PAYLOAD_TOO_LARGE {
		t.Errorf("file access reason = %v, want PAYLOAD_TOO_LARGE", got)
	}
}

// fileHandler mirrors the instance manager's file cap (inclusive): a read of a
// path named by a size returns that many bytes, and both a read and an edit past
// the cap are refused as too large.
func fileHandler(maxFileBytes int, edited *atomic.Int64) *scriptedHandler {
	tooLarge := session.CommandResult{
		ErrorCode:        session.CommandErrorFileAccessDenied,
		FileAccessReason: session.FileAccessReasonPayloadTooLarge,
		ErrorMessage:     "exceeds the cap",
	}
	return newScriptedHandler(func(cmd session.Command) session.CommandResult {
		switch cmd.Kind {
		case "ReadFile":
			size, _ := strconv.Atoi(cmd.Path)
			if size > maxFileBytes {
				return tooLarge
			}
			return session.CommandResult{Success: true, FileContent: make([]byte, size)}
		case "EditFile":
			if len(cmd.Content) > maxFileBytes {
				return tooLarge
			}
			edited.Store(int64(len(cmd.Content)))
			return session.CommandResult{Success: true}
		}
		return session.CommandResult{ErrorMessage: "unexpected " + cmd.Kind}
	})
}

func TestAFileReadAtTheFileCapCrossesTheStream(t *testing.T) {
	maxFile := readLimits(t).MaxFileBytes
	api := newAPIServer()
	runWorker(t, api, fileHandler(maxFile, new(atomic.Int64)))

	for _, size := range []int{maxFile - 1, maxFile} {
		result := api.roundTrip(t, readFile(strconv.Itoa(size)))
		if !result.GetSuccess() {
			t.Fatalf("read of %d bytes failed: %v", size, result.GetError())
		}
		if got := len(result.GetFileContent()); got != size {
			t.Fatalf("read of %d bytes returned %d", size, got)
		}
		api.assertSessionSurvived(t)
	}

	assertPayloadTooLarge(t, api.roundTrip(t, readFile(strconv.Itoa(maxFile+1))))
	api.assertSessionSurvived(t)
}

func TestAFileEditAtTheFileCapCrossesTheStream(t *testing.T) {
	maxFile := readLimits(t).MaxFileBytes
	api := newAPIServer()
	var edited atomic.Int64
	runWorker(t, api, fileHandler(maxFile, &edited))

	edit := func(size int) *controlplanev1.CommandResult {
		return api.roundTrip(t, &controlplanev1.ApiCommand{
			CommandId: "cmd-1", ServerId: "server-a",
			Command: &controlplanev1.ApiCommand_EditFile{EditFile: &controlplanev1.EditFile{
				// A path as long as Linux allows (PATH_MAX) still fits beside the body.
				Path: strings.Repeat("d/", 2047) + "f", Content: make([]byte, size),
			}},
		})
	}
	for _, size := range []int{maxFile - 1, maxFile} {
		if result := edit(size); !result.GetSuccess() {
			t.Fatalf("edit of %d bytes failed: %v", size, result.GetError())
		}
		if got := edited.Load(); got != int64(size) {
			t.Fatalf("the handler was handed %d bytes, want %d", got, size)
		}
		api.assertSessionSurvived(t)
	}

	assertPayloadTooLarge(t, edit(maxFile+1))
	api.assertSessionSurvived(t)
}

// Each result kind that can carry a tenant-sized payload, made over-limit by a
// handler that ignores its own caps: the command fails (or, for a listing, is
// clipped — truncation is that result's contract) and the stream carries on.
func TestAnOverLimitResultFailsOnlyItsCommand(t *testing.T) {
	huge := controlplane.MaxMessageBytes
	entries := make([]session.FileEntry, 40_000)
	for i := range entries {
		entries[i] = session.FileEntry{Name: strings.Repeat("n", 255), Size: 1}
	}

	cases := []struct {
		name   string
		result session.CommandResult
		check  func(t *testing.T, result *controlplanev1.CommandResult)
	}{
		{
			name:   "file content",
			result: session.CommandResult{Success: true, FileContent: make([]byte, huge)},
			check:  assertPayloadTooLarge,
		},
		{
			name:   "directory listing",
			result: session.CommandResult{Success: true, FileListing: &session.FileListing{Entries: entries}},
			check: func(t *testing.T, result *controlplanev1.CommandResult) {
				listing := result.GetFileListing()
				if !result.GetSuccess() || !listing.GetTruncated() {
					t.Fatalf("success = %v, truncated = %v, want a clipped listing", result.GetSuccess(), listing.GetTruncated())
				}
				if got := len(listing.GetEntries()); got == 0 || got >= len(entries) {
					t.Fatalf("the clipped listing holds %d of %d entries", got, len(entries))
				}
			},
		},
		{
			name:   "console output",
			result: session.CommandResult{Success: true, Output: strings.Repeat("o", huge)},
			check: func(t *testing.T, result *controlplanev1.CommandResult) {
				if result.GetSuccess() || result.GetError().GetCode() != controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_INTERNAL {
					t.Fatalf("success = %v, code = %v, want an INTERNAL failure", result.GetSuccess(), result.GetError().GetCode())
				}
			},
		},
		{
			name: "error message",
			result: session.CommandResult{
				ErrorCode: session.CommandErrorInvalidState, ErrorMessage: strings.Repeat("e", huge),
			},
			check: func(t *testing.T, result *controlplanev1.CommandResult) {
				if result.GetSuccess() || result.GetError().GetCode() != controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_INVALID_STATE {
					t.Fatalf("success = %v, code = %v, want the command's own INVALID_STATE", result.GetSuccess(), result.GetError().GetCode())
				}
				if got := len(result.GetError().GetMessage()); got > 1024 {
					t.Fatalf("the failure still carries a %d-byte message", got)
				}
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			api := newAPIServer()
			logs := runWorker(t, api, newScriptedHandler(func(session.Command) session.CommandResult { return tc.result }))

			result := api.roundTrip(t, readFile("anything"))

			tc.check(t, result)
			if size := proto.Size(result); size > controlplane.MaxMessageBytes {
				t.Fatalf("the result that was sent is %d bytes, over the limit", size)
			}
			warned := false
			for _, line := range strings.Split(logs.String(), "\n") {
				if strings.Contains(line, "message limit") {
					warned = strings.Contains(line, "level=WARN") &&
						strings.Contains(line, "kind=ReadFile") && strings.Contains(line, "server_id=server-a")
					if len(line) > 1024 {
						t.Fatalf("the warning is %d bytes long; it must not carry the payload", len(line))
					}
				}
			}
			if !warned {
				t.Fatalf("no WARN naming the command kind and server id; log:\n%.2000s", logs.String())
			}
			api.assertSessionSurvived(t)
		})
	}
}

// An over-limit event is not a command, so there is no result to fail: a log line
// is dropped and a status change is sent without its detail. Neither can be
// produced over-limit honestly (a log line is capped at 8 KiB at the source, and
// a detail is a short Worker-authored sentence); the events are fed in directly.
func TestAnOverLimitEventDoesNotEndTheSession(t *testing.T) {
	huge := strings.Repeat("x", controlplane.MaxMessageBytes)
	api := newAPIServer()
	handler := newScriptedHandler(func(session.Command) session.CommandResult { return session.CommandResult{} })
	logs := runWorker(t, api, handler)
	next := func() *controlplanev1.Event {
		t.Helper()
		select {
		case msg := <-api.events:
			return msg.GetEvent()
		case <-time.After(10 * time.Second):
			t.Fatal("no event arrived (session down?)")
			return nil
		}
	}

	handler.logs <- session.LogEvent{ServerID: "server-a", Line: huge}
	handler.logs <- session.LogEvent{ServerID: "server-a", Line: "after"}
	if got := next().GetLogLine().GetLine(); got != "after" {
		t.Fatalf("the first log line to arrive is %d bytes long, want the over-limit one dropped", len(got))
	}

	handler.events <- session.StatusEvent{ServerID: "server-a", State: "crashed", Detail: huge}
	event := next()
	if got := event.GetStatusChange().GetState(); got != controlplanev1.ServerState_SERVER_STATE_CRASHED {
		t.Fatalf("state = %v, want the transition itself still reported", got)
	}
	if got := event.GetStatusChange().GetDetail(); got != "" {
		t.Fatalf("the status change still carries a %d-byte detail", len(got))
	}

	if got := strings.Count(logs.String(), "message limit"); got != 2 {
		t.Fatalf("%d warnings, want one per over-limit event", got)
	}
	if !strings.Contains(logs.String(), "server_id=server-a") {
		t.Fatal("the warning does not name the server id")
	}
	api.assertSessionSurvived(t)
}

// --- What gRPC itself does to an over-limit message -------------------------
//
// Either direction ends the WHOLE stream with RESOURCE_EXHAUSTED: there is no
// per-message refusal to recover from. That is why each side checks the encoded
// size before it sends.

// rawSession opens a Session directly on conn and registers, bypassing the
// adapter's own size check.
func rawSession(t *testing.T, conn *grpc.ClientConn) controlplanev1.WorkerService_SessionClient {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	stream, err := controlplanev1.NewWorkerServiceClient(conn).Session(ctx)
	if err != nil {
		t.Fatalf("open session: %v", err)
	}
	if err := stream.Send(&controlplanev1.WorkerMessage{
		Payload: &controlplanev1.WorkerMessage_Register{Register: &controlplanev1.Register{WorkerId: "w"}},
	}); err != nil {
		t.Fatalf("send register: %v", err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatalf("recv ack: %v", err)
	}
	return stream
}

func overLimitResult() *controlplanev1.WorkerMessage {
	return &controlplanev1.WorkerMessage{
		CorrelationId: "cmd-1",
		Payload: &controlplanev1.WorkerMessage_CommandResult{CommandResult: &controlplanev1.CommandResult{
			Success: true,
			Result:  &controlplanev1.CommandResult_FileContent{FileContent: make([]byte, controlplane.MaxMessageBytes)},
		}},
	}
}

func TestGRPCEndsTheStreamWhenTheWorkerSendsAnOverLimitMessage(t *testing.T) {
	stream := rawSession(t, newAPIServer().start(t, limitedAPI(), controlplane.MessageSizeDialOption()))

	err := stream.Send(overLimitResult())

	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("Send error = %v, want RESOURCE_EXHAUSTED from the client's own send limit", err)
	}
	// The refused Send finished the stream: nothing else can be read from it.
	if _, err := stream.Recv(); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("Recv after the refused Send = %v, want the stream ended with RESOURCE_EXHAUSTED", err)
	}
}

func TestGRPCEndsTheStreamWhenTheAPIReceivesAnOverLimitMessage(t *testing.T) {
	// No client-side limit: the message leaves the Worker and the API refuses it.
	stream := rawSession(t, newAPIServer().start(t, limitedAPI()))

	if err := stream.Send(overLimitResult()); err != nil {
		t.Fatalf("Send: %v", err)
	}

	if _, err := stream.Recv(); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("Recv = %v, want the API to end the stream with RESOURCE_EXHAUSTED", err)
	}
}

func TestGRPCEndsTheStreamWhenTheWorkerReceivesAnOverLimitMessage(t *testing.T) {
	// No server-side send limit: the command leaves the API and the Worker's
	// receive limit refuses it.
	api := newAPIServer()
	stream := rawSession(t, api.start(t, nil, controlplane.MessageSizeDialOption()))

	api.commands <- &controlplanev1.ApiCommand{
		CommandId: "cmd-1", ServerId: "server-a",
		Command: &controlplanev1.ApiCommand_EditFile{EditFile: &controlplanev1.EditFile{
			Path: "a", Content: make([]byte, controlplane.MaxMessageBytes),
		}},
	}

	if _, err := stream.Recv(); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("Recv = %v, want RESOURCE_EXHAUSTED from the client's receive limit", err)
	}
}
