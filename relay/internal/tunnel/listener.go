package tunnel

import (
	"bufio"
	"context"
	"crypto/tls"
	"errors"
	"io"
	"log/slog"
	"net"
	"strings"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/netutil"
)

// dialHandshakeDeadline bounds how long a tunnel connection has to send its
// handshake line before it is dropped (RELAY.md Section 5).
const dialHandshakeDeadline = 5 * time.Second

// maxHandshakeBytes hard-caps the pre-auth handshake read on the
// internet-exposed tunnel listener. The whole legal handshake
// ("MCSD-TUNNEL/1\n<128-bit token>\n") is well under this; a peer that streams
// more, or never sends a newline, is dropped silently rather than allowed to
// buffer unbounded data before authenticating.
const maxHandshakeBytes = 256

// handshakePrefix is the fixed first line of the Worker dial-back handshake
// (RELAY.md Section 5): "MCSD-TUNNEL/1\n" followed by "<token>\n".
const handshakePrefix = "MCSD-TUNNEL/1"

// Listener accepts the Worker's TLS dial-backs, matches the presented token
// against the table of waiting player connections, and hands the connection to
// the waiter (RELAY.md Section 5).
type Listener struct {
	ln     net.Listener
	tokens *TokenTable
	caps   *ipcaps.IPCaps
	logger *slog.Logger
}

// NewListener binds the tunnel listener on addr with the given TLS config. caps
// enforces the per-IP concurrent-connection cap (RELAY.md Section 11); only its
// connection cap is used, since the tunnel has no join concept.
func NewListener(addr string, tlsCfg *tls.Config, tokens *TokenTable, caps *ipcaps.IPCaps, logger *slog.Logger) (*Listener, error) {
	ln, err := tls.Listen("tcp", addr, tlsCfg)
	if err != nil {
		return nil, err
	}
	return &Listener{ln: ln, tokens: tokens, caps: caps, logger: logger}, nil
}

// Addr returns the listener's bound address.
func (l *Listener) Addr() net.Addr { return l.ln.Addr() }

// Serve accepts connections until ctx is cancelled or the listener closes.
func (l *Listener) Serve(ctx context.Context) error {
	go func() {
		<-ctx.Done()
		_ = l.ln.Close()
	}()
	for {
		conn, err := l.ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		go l.handle(conn)
	}
}

// handle reads the dial-back handshake and either delivers the connection to a
// waiting player or closes it without a response (RELAY.md Section 5).
func (l *Listener) handle(conn net.Conn) {
	ip := netutil.HostOf(conn.RemoteAddr())

	// Per-IP concurrent-connection cap (RELAY.md Section 11): bound how many
	// unauthenticated handshake windows one source IP can hold. Over the cap is a
	// silent close, matching the tunnel's bad-token behaviour. The slot is held
	// only for the pre-auth window — once the handshake either fails or the conn
	// is delivered to a waiter, the slot is released, since a delivered
	// connection is authenticated and accounted on the game side.
	if !l.caps.Acquire(ip) {
		_ = conn.Close()
		return
	}
	defer l.caps.Release(ip)

	token, ok := readHandshake(conn)
	if !ok {
		_ = conn.Close()
		return
	}
	if !l.tokens.Deliver(token, conn) {
		// Unknown, expired, or reused token: close without a response.
		l.logger.Debug("tunnel dial-back rejected: no waiter for token")
		_ = conn.Close()
	}
	// On a successful Deliver the waiter owns conn and closes it when the splice
	// ends.
}

// readHandshake parses the "MCSD-TUNNEL/1\n<token>\n" handshake within the
// deadline. It returns the token on success. The connection's read deadline is
// cleared on success so the subsequent splice is not bounded.
func readHandshake(conn net.Conn) (string, bool) {
	_ = conn.SetReadDeadline(time.Now().Add(dialHandshakeDeadline))
	// Hard-cap the pre-auth read: a bufio.Reader sized to maxHandshakeBytes over a
	// LimitReader of the same size. ReadSlice returns ErrBufferFull if a line
	// exceeds the buffer (no newline within the cap), and the LimitReader yields
	// io.EOF once the cap is consumed across both lines — either way an over-long
	// or newline-free handshake is dropped silently.
	r := bufio.NewReaderSize(io.LimitReader(conn, maxHandshakeBytes), maxHandshakeBytes)

	first, err := r.ReadSlice('\n')
	if err != nil || strings.TrimRight(string(first), "\n") != handshakePrefix {
		return "", false
	}
	tokenLine, err := r.ReadSlice('\n')
	if err != nil {
		return "", false
	}
	token := strings.TrimRight(string(tokenLine), "\n")
	if token == "" {
		return "", false
	}
	// Any bytes buffered past the handshake belong to the spliced stream; this
	// handshake protocol sends nothing further before "OK\n", so a well-behaved
	// Worker leaves the buffer empty.
	if r.Buffered() > 0 {
		return "", false
	}
	_ = conn.SetReadDeadline(time.Time{})
	return token, true
}

// ConfirmAndAttach writes the "OK\n" acknowledgement to a delivered tunnel
// connection, after which the player goroutine splices. It is the player side's
// step (the waiter), separated so the listener only routes. A write error
// closes the connection.
func ConfirmAndAttach(conn net.Conn) error {
	if _, err := conn.Write([]byte("OK\n")); err != nil {
		_ = conn.Close()
		return errors.New("tunnel: write OK ack failed")
	}
	return nil
}
