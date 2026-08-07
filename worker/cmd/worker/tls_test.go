package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/config"
)

// writeSelfSignedCertKey writes a fresh self-signed cert/key PEM pair into dir
// and returns their paths. The pair is a valid tls.LoadX509KeyPair input so the
// mTLS client-cert branch under test loads real material, not a stub.
func writeSelfSignedCertKey(t *testing.T, dir string) (certPath, keyPath string) {
	t.Helper()

	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "test"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create certificate: %v", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("marshal key: %v", err)
	}

	certPath = filepath.Join(dir, "cert.pem")
	keyPath = filepath.Join(dir, "key.pem")
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	if err := os.WriteFile(certPath, certPEM, 0o600); err != nil {
		t.Fatalf("write cert: %v", err)
	}
	if err := os.WriteFile(keyPath, keyPEM, 0o600); err != nil {
		t.Fatalf("write key: %v", err)
	}
	return certPath, keyPath
}

// TestBuildTLSConfigWithoutClientPair pins the control-channel TLS assembly with
// only a CA bundle: RootCAs is populated, TLS 1.3 is floored, and no client
// certificate is loaded (the mTLS branch stays inactive).
func TestBuildTLSConfigWithoutClientPair(t *testing.T) {
	dir := t.TempDir()
	caPath, _ := writeSelfSignedCertKey(t, dir)

	cfg, err := buildTLSConfig(config.TLSConfig{CAFile: caPath})
	if err != nil {
		t.Fatalf("buildTLSConfig() error = %v, want nil", err)
	}
	if cfg.RootCAs == nil {
		t.Error("buildTLSConfig() RootCAs = nil, want the CA pool populated")
	}
	if cfg.MinVersion != tls.VersionTLS13 {
		t.Errorf("buildTLSConfig() MinVersion = %x, want %x", cfg.MinVersion, tls.VersionTLS13)
	}
	if len(cfg.Certificates) != 0 {
		t.Errorf("buildTLSConfig() Certificates = %d, want 0 (no mTLS pair configured)", len(cfg.Certificates))
	}
}

// TestBuildTLSConfigWithClientPair pins the mTLS branch: when both
// client_cert_file and client_key_file are set, the loaded client certificate
// is attached to the config. A regression dropping this branch would compile,
// pass CI, and surface only as a rejected mTLS handshake at runtime (#1981).
func TestBuildTLSConfigWithClientPair(t *testing.T) {
	dir := t.TempDir()
	caPath, _ := writeSelfSignedCertKey(t, dir)
	clientCert, clientKey := writeSelfSignedCertKey(t, mkdir(t, dir, "client"))

	cfg, err := buildTLSConfig(config.TLSConfig{
		CAFile:         caPath,
		ClientCertFile: clientCert,
		ClientKeyFile:  clientKey,
	})
	if err != nil {
		t.Fatalf("buildTLSConfig() error = %v, want nil", err)
	}
	if len(cfg.Certificates) != 1 {
		t.Errorf("buildTLSConfig() Certificates = %d, want 1 (mTLS client cert loaded)", len(cfg.Certificates))
	}
}

// TestBuildTLSConfigHalfClientPairIsRejected flips the #1981 pin: a
// half-configured mTLS client pair (only one of client_cert_file /
// client_key_file) is now a fatal config-validation error, so buildTLSConfig
// never sees one. The full both-direction / accept-side coverage lives in the
// config package's validator tests; this pins that main's Load path rejects it
// and names the missing half (issue #2661).
func TestBuildTLSConfigHalfClientPairIsRejected(t *testing.T) {
	env := map[string]string{
		"MCD_WORKER_API_GRPC_ENDPOINT":        "api:50051",
		"MCD_WORKER_API_CREDENTIAL":           "secret",
		"MCD_WORKER_API_TLS_CA_FILE":          "/etc/ssl/ca.pem",
		"MCD_WORKER_API_TLS_CLIENT_CERT_FILE": "/etc/ssl/client.pem",
		"MCD_WORKER_WORKER_SCRATCH_DIR":       t.TempDir(),
		"MCD_WORKER_WORKER_DRIVERS":           "container",
		"MCD_WORKER_DRIVER_CONTAINER_IMAGES":  "21=eclipse-temurin:21-jre",
	}

	_, err := config.Load("", func(k string) string { return env[k] })
	if err == nil {
		t.Fatal("config.Load() with a half mTLS client pair: want error, got nil")
	}
	if !strings.Contains(err.Error(), "client_key_file") {
		t.Errorf("error %q does not name the missing client_key_file", err.Error())
	}
}

// TestBuildTLSConfigCAErrors pins the CA read-failure and no-usable-certs paths.
func TestBuildTLSConfigCAErrors(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "missing.pem")
	emptyBundle := filepath.Join(dir, "empty.pem")
	if err := os.WriteFile(emptyBundle, []byte("not a pem"), 0o600); err != nil {
		t.Fatalf("write empty bundle: %v", err)
	}

	tests := []struct {
		name string
		tls  config.TLSConfig
	}{
		{name: "unreadable CA file", tls: config.TLSConfig{CAFile: missing}},
		{name: "CA file with no usable certs", tls: config.TLSConfig{CAFile: emptyBundle}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := buildTLSConfig(tc.tls); err == nil {
				t.Fatal("buildTLSConfig() error = nil, want a CA error")
			}
		})
	}
}

// TestBuildTLSConfigClientLoadFailure pins the mTLS load-failure path: both
// client paths set but pointing at unloadable material.
func TestBuildTLSConfigClientLoadFailure(t *testing.T) {
	dir := t.TempDir()
	caPath, _ := writeSelfSignedCertKey(t, dir)
	missing := filepath.Join(dir, "nope")

	if _, err := buildTLSConfig(config.TLSConfig{
		CAFile:         caPath,
		ClientCertFile: missing,
		ClientKeyFile:  missing,
	}); err == nil {
		t.Fatal("buildTLSConfig() error = nil, want an mTLS load error")
	}
}

// TestBuildTransferClient pins the data-plane HTTP client's TLS posture: it
// mirrors the control channel — no CA file yields a plaintext transport, a CA
// file yields a TLS transport, and a CA error propagates.
func TestBuildTransferClient(t *testing.T) {
	dir := t.TempDir()
	caPath, _ := writeSelfSignedCertKey(t, dir)
	missing := filepath.Join(dir, "missing.pem")

	t.Run("insecure client leaves TLS unset", func(t *testing.T) {
		c, err := buildTransferClient(config.APIConfig{TLS: config.TLSConfig{Insecure: true}})
		if err != nil {
			t.Fatalf("buildTransferClient() error = %v, want nil", err)
		}
		if tr := c.Transport.(*http.Transport); tr.TLSClientConfig != nil {
			t.Error("buildTransferClient() TLSClientConfig set, want nil for a plaintext client")
		}
	})

	t.Run("CA file yields a TLS transport", func(t *testing.T) {
		c, err := buildTransferClient(config.APIConfig{TLS: config.TLSConfig{CAFile: caPath}})
		if err != nil {
			t.Fatalf("buildTransferClient() error = %v, want nil", err)
		}
		if tr := c.Transport.(*http.Transport); tr.TLSClientConfig == nil {
			t.Error("buildTransferClient() TLSClientConfig = nil, want a TLS config")
		}
	})

	t.Run("CA error propagates", func(t *testing.T) {
		if _, err := buildTransferClient(config.APIConfig{TLS: config.TLSConfig{CAFile: missing}}); err == nil {
			t.Fatal("buildTransferClient() error = nil, want a CA read error")
		}
	})
}

// TestDialGate pins the worker control-plane dial's CA-file/insecure gate
// (CONFIGURATION.md Section 6.1). grpc.NewClient is lazy, so a returned conn
// means the credentials branch was selected without a live dial.
func TestDialGate(t *testing.T) {
	dir := t.TempDir()
	caPath, _ := writeSelfSignedCertKey(t, dir)
	missing := filepath.Join(dir, "missing.pem")

	logger := newLogger(config.LogConfig{})

	t.Run("insecure dial with no CA file", func(t *testing.T) {
		conn, err := dial(config.APIConfig{GRPCEndpoint: "127.0.0.1:1", TLS: config.TLSConfig{Insecure: true}}, logger)
		if err != nil {
			t.Fatalf("dial() error = %v, want nil", err)
		}
		_ = conn.Close()
	})

	t.Run("TLS dial with a valid CA file", func(t *testing.T) {
		conn, err := dial(config.APIConfig{GRPCEndpoint: "127.0.0.1:1", TLS: config.TLSConfig{CAFile: caPath}}, logger)
		if err != nil {
			t.Fatalf("dial() error = %v, want nil", err)
		}
		_ = conn.Close()
	})

	t.Run("unreadable CA file is a fatal error", func(t *testing.T) {
		if _, err := dial(config.APIConfig{GRPCEndpoint: "127.0.0.1:1", TLS: config.TLSConfig{CAFile: missing}}, logger); err == nil {
			t.Fatal("dial() error = nil, want a CA read error")
		}
	})
}

// mkdir makes a subdirectory under parent and returns its path, so a second
// cert/key pair can be written without colliding with the first pair's names.
func mkdir(t *testing.T, parent, name string) string {
	t.Helper()
	p := filepath.Join(parent, name)
	if err := os.Mkdir(p, 0o700); err != nil {
		t.Fatalf("mkdir %q: %v", p, err)
	}
	return p
}
