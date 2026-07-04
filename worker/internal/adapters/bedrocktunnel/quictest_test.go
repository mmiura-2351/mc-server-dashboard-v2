package bedrocktunnel

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"log/slog"
	"math/big"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/quic-go/quic-go"
	"google.golang.org/protobuf/proto"

	bedrocktunnelv1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/bedrocktunnel/v1"
)

// discardLogger writes nowhere, keeping test output clean.
func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// selfSignedTLS builds a one-off self-signed TLS config (leaf is its own CA,
// fine for a test root) for loopback QUIC tests, mirroring
// relay/internal/bedrock/quictest_test.go's helper (the relay side of this
// same wire contract) — plus the cert's PEM encoding, since (unlike the
// relay's own tests) the code under test here is the QUIC *client* and
// verifies the server's certificate against Spec.CAPEM rather than skipping
// verification.
func selfSignedTLS(t *testing.T) (*tls.Config, string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "bedrocktunnel-test"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IsCA:         true,
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	cert := tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
	tlsCfg := &tls.Config{Certificates: []tls.Certificate{cert}, NextProtos: []string{ALPN}, MinVersion: tls.VersionTLS13}
	return tlsCfg, string(certPEM)
}

// acceptResult is one accepted-and-handshaked connection a fakeRelay hands to
// the test, plus the TunnelHello it validated.
type acceptResult struct {
	conn  *quic.Conn
	hello *bedrocktunnelv1.TunnelHello
}

// fakeRelay is a real loopback QUIC listener standing in for the relay's
// Bedrock tunnel listener: it runs the TunnelHello/TunnelHelloAck handshake
// exactly as docs/app/BEDROCK_TUNNEL.md Section 4 specifies, deciding
// accept/reject via acceptFn, and hands every accepted connection to the test
// over the accepted channel.
type fakeRelay struct {
	t        *testing.T
	ln       *quic.Listener
	caPEM    string
	acceptFn func(hello *bedrocktunnelv1.TunnelHello) (bool, string)

	accepted chan acceptResult

	mu      sync.Mutex
	helloes []*bedrocktunnelv1.TunnelHello
}

// newFakeRelay starts a fakeRelay bound to loopback with a fresh self-signed
// cert. acceptFn decides accept/reject per TunnelHello; nil accepts everything.
func newFakeRelay(t *testing.T, acceptFn func(*bedrocktunnelv1.TunnelHello) (bool, string)) *fakeRelay {
	t.Helper()
	tlsCfg, caPEM := selfSignedTLS(t)
	ln, err := quic.ListenAddr("127.0.0.1:0", tlsCfg, &quic.Config{EnableDatagrams: true})
	if err != nil {
		t.Fatalf("quic.ListenAddr: %v", err)
	}
	r := &fakeRelay{t: t, ln: ln, caPEM: caPEM, acceptFn: acceptFn, accepted: make(chan acceptResult, 8)}
	go r.serve()
	t.Cleanup(func() { _ = ln.Close() })
	return r
}

func (r *fakeRelay) addr() string { return r.ln.Addr().String() }

func (r *fakeRelay) serve() {
	for {
		conn, err := r.ln.Accept(context.Background())
		if err != nil {
			return
		}
		go r.handle(conn)
	}
}

// handle runs the handshake for one Worker dial-out: accept the first
// bidirectional stream, read TunnelHello, decide accept/reject, write
// TunnelHelloAck, close the stream. On acceptance the connection is handed to
// the test via r.accepted; on rejection the connection is closed, mirroring
// the relay's own posture (docs/app/BEDROCK_TUNNEL.md Section 3: "connection
// closed by relay on rejection").
func (r *fakeRelay) handle(conn *quic.Conn) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	stream, err := conn.AcceptStream(ctx)
	if err != nil {
		_ = conn.CloseWithError(0, "no handshake stream")
		return
	}
	data, err := readFramed(stream, maxHandshakeMessageBytes)
	if err != nil {
		_ = conn.CloseWithError(0, "bad hello")
		return
	}
	var hello bedrocktunnelv1.TunnelHello
	if err := proto.Unmarshal(data, &hello); err != nil {
		_ = conn.CloseWithError(0, "bad hello")
		return
	}
	r.mu.Lock()
	r.helloes = append(r.helloes, &hello)
	r.mu.Unlock()

	accept, reason := true, ""
	if r.acceptFn != nil {
		accept, reason = r.acceptFn(&hello)
	}
	ackData, err := proto.Marshal(&bedrocktunnelv1.TunnelHelloAck{Accepted: accept, RejectReason: reason})
	if err != nil {
		_ = conn.CloseWithError(0, "marshal ack failed")
		return
	}
	if err := writeFramed(stream, ackData); err != nil {
		_ = conn.CloseWithError(0, "write ack failed")
		return
	}
	_ = stream.Close()

	if !accept {
		_ = conn.CloseWithError(0, reason)
		return
	}
	r.accepted <- acceptResult{conn: conn, hello: &hello}
}

// waitAccepted blocks for the next accepted connection, failing the test if
// none arrives within the bound.
func (r *fakeRelay) waitAccepted(t *testing.T) acceptResult {
	t.Helper()
	select {
	case a := <-r.accepted:
		return a
	case <-time.After(5 * time.Second):
		t.Fatal("no accepted connection from fakeRelay within 5s")
		return acceptResult{}
	}
}

// helloCount returns how many TunnelHello messages the fakeRelay has read so
// far (accepted or rejected).
func (r *fakeRelay) helloCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.helloes)
}

// waitConnDone blocks until conn's context is done (CONNECTION_CLOSE
// observed), failing the test if it does not happen within the bound.
func waitConnDone(t *testing.T, conn *quic.Conn) {
	t.Helper()
	select {
	case <-conn.Context().Done():
	case <-time.After(5 * time.Second):
		t.Fatal("expected the QUIC connection to close within 5s")
	}
}
