package instancemanager

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/rcon"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// A running server whose RCON peer answers the handshake but never answers
// save-off fails that leg at rcon's 30 s per-leg ceiling, not at the snapshot
// command's deadline (issue #4031). The quiesce then refuses the snapshot
// quiesce_unavailable after one ceiling, and the save-on owed for a save-off that
// may have landed is still attempted. Before, the leg ran to the command's
// deadline, here 630 s (a 660 s transfer deadline less the restore's 30 s),
// holding the server's lane and a command slot for all of it. The real rcon
// client runs over in-memory connections on synctest's fake clock.
func TestSnapshotQuiesceRefusesAHungSaveOffAtTheRCONCeiling(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		quiesceConn, quiescePeer := net.Pipe()
		go rconPeer(quiescePeer, nil)
		restoreConn, restorePeer := net.Pipe()
		answered := make(chan string, 4)
		go rconPeer(restorePeer, answered)
		conns := make(chan net.Conn, 2)
		conns <- quiesceConn
		conns <- restoreConn
		// A connection the manager never dialed would leave its peer blocked.
		t.Cleanup(func() {
			_ = quiesceConn.Close()
			_ = restoreConn.Close()
		})

		m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{})
		m.openControl = func(ctx context.Context, _, _, _ string) (execution.ServerControl, error) {
			select {
			case conn := <-conns:
				c, err := rcon.NewClient(ctx, conn, "pipe", "secret")
				if err != nil {
					return nil, err
				}
				// Hidden behind a plain ServerControl, so the restore dials here
				// rather than through Redial's TCP dial, which has no peer here.
				return struct{ execution.ServerControl }{c}, nil
			default:
				return nil, errors.New("test: no RCON peer left to dial")
			}
		}
		m.SetTransferDeadline(660 * time.Second)
		startRunning(t, m)

		start := time.Now()
		res := m.Handle(context.Background(), snapshotCmd())
		if elapsed := time.Since(start); elapsed != 30*time.Second {
			t.Fatalf("snapshot answered after %v, want 30s: one RCON leg's ceiling", elapsed)
		}
		if res.Success || !strings.Contains(res.ErrorMessage, "quiesce_unavailable") {
			t.Fatalf("SnapshotTrigger = %+v, want refused quiesce_unavailable", res)
		}
		select {
		case line := <-answered:
			if line != "save-on" {
				t.Fatalf("restore peer got %q first, want save-on", line)
			}
		default:
			t.Fatal("no save-on attempted for a save-off that may have landed")
		}
	})
}

// rconPeer serves the server end of an in-memory RCON connection. It accepts any
// password. With answered set it answers every command, sending the command's
// body there first; with answered nil it never answers one. It returns once the
// client closes the connection.
func rconPeer(conn net.Conn, answered chan<- string) {
	defer func() { _ = conn.Close() }()
	const (
		typeResponseValue = 0
		typeExecCommand   = 2
		typeAuthResponse  = 2
		typeAuth          = 3
	)
	for {
		var head [12]byte // length, id, type
		if _, err := io.ReadFull(conn, head[:]); err != nil {
			return
		}
		body := make([]byte, binary.LittleEndian.Uint32(head[0:4])-8)
		if _, err := io.ReadFull(conn, body); err != nil {
			return
		}
		reply := uint32(typeResponseValue)
		switch binary.LittleEndian.Uint32(head[8:12]) {
		case typeAuth:
			reply = typeAuthResponse
		case typeExecCommand:
			if answered == nil {
				continue
			}
			answered <- string(body[:len(body)-2]) // the two trailing NULs
		}
		// An empty-bodied reply with the request's id: length 10, then the id,
		// the type and the two NULs.
		frame := make([]byte, 14)
		binary.LittleEndian.PutUint32(frame[0:4], 10)
		copy(frame[4:8], head[4:8])
		binary.LittleEndian.PutUint32(frame[8:12], reply)
		if _, err := conn.Write(frame); err != nil {
			return
		}
	}
}
