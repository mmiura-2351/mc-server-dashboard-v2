package bedrock

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"math/big"
	"net"
	"testing"
	"time"

	"github.com/quic-go/quic-go"
)

// selfSignedTLS builds a one-off self-signed TLS config for loopback QUIC
// tests, mirroring relay/test/integration_test.go's helper for the TCP tunnel
// listener.
func selfSignedTLS(t *testing.T) *tls.Config {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "bedrock-test"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert := tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
	return &tls.Config{Certificates: []tls.Certificate{cert}, NextProtos: []string{ALPN}, MinVersion: tls.VersionTLS13}
}

// clientTLS builds a client-side TLS config that skips verification (test
// only -- production dials verify against tls_ca_pem, worker-side, #1546).
func clientTLS() *tls.Config {
	return &tls.Config{NextProtos: []string{ALPN}, InsecureSkipVerify: true, MinVersion: tls.VersionTLS13} //nolint:gosec
}

// newQUICListener binds a bare quic.Listener for tests that drive the
// handshake themselves (bypassing bedrock.Listener), returning it plus a
// teardown func.
func newQUICListener(t *testing.T) *quic.Listener {
	t.Helper()
	ln, err := quic.ListenAddr("127.0.0.1:0", selfSignedTLS(t), &quic.Config{EnableDatagrams: true})
	if err != nil {
		t.Fatalf("quic.ListenAddr: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	return ln
}

// dialQUIC dials addr as a Worker would, returning the client-side connection.
func dialQUIC(ctx context.Context, t *testing.T, addr string) *quic.Conn {
	t.Helper()
	conn, err := quic.DialAddr(ctx, addr, clientTLS(), &quic.Config{EnableDatagrams: true})
	if err != nil {
		t.Fatalf("quic.DialAddr(%q): %v", addr, err)
	}
	t.Cleanup(func() { _ = conn.CloseWithError(0, "test done") })
	return conn
}

// quicConnPair returns a connected (server, client) *quic.Conn pair over
// loopback, both with RFC 9221 datagrams enabled -- a real "fake QUIC peer"
// for tests that exercise Tunnel directly without the handshake layer.
func quicConnPair(t *testing.T) (server, client *quic.Conn) {
	t.Helper()
	ln := newQUICListener(t)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	client = dialQUIC(ctx, t, ln.Addr().String())

	acceptCtx, acceptCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer acceptCancel()
	server, err := ln.Accept(acceptCtx)
	if err != nil {
		t.Fatalf("Accept: %v", err)
	}
	t.Cleanup(func() { _ = server.CloseWithError(0, "test done") })
	return server, client
}
