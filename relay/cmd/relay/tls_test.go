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
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/config"
)

// writeSelfSignedCertKey writes a fresh self-signed cert/key PEM pair into dir
// and returns their paths. The pair is a valid tls.LoadX509KeyPair input so the
// TLS-load branches under test see real material, not a hand-rolled stub.
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

// TestAdvertisedTunnelCA pins the three-way switch in advertisedTunnelCA
// (RELAY.md Section 5): which CA PEM the relay advertises to Workers keyed on
// tunnel.tls.advertised_ca_file. The SystemRootsCA → "" case is the one whose
// silent regression (a swapped case or a broken sentinel compare) would make
// the relay advertise its self-signed listener cert and break every dial-back
// from a system-roots Worker in production (issue #1981).
func TestAdvertisedTunnelCA(t *testing.T) {
	dir := t.TempDir()
	certPath, _ := writeSelfSignedCertKey(t, dir)
	certPEM, err := os.ReadFile(certPath)
	if err != nil {
		t.Fatalf("read cert back: %v", err)
	}

	advertisedPath := filepath.Join(dir, "advertised-ca.pem")
	advertisedPEM := []byte("-----BEGIN CERTIFICATE-----\nADVERTISED\n-----END CERTIFICATE-----\n")
	if err := os.WriteFile(advertisedPath, advertisedPEM, 0o600); err != nil {
		t.Fatalf("write advertised CA: %v", err)
	}

	tests := []struct {
		name string
		tls  config.TunnelTLSConfig
		want string
	}{
		{
			name: "unset advertises the listener cert PEM",
			tls:  config.TunnelTLSConfig{CertFile: certPath},
			want: string(certPEM),
		},
		{
			name: "SystemRootsCA advertises an empty bundle",
			tls:  config.TunnelTLSConfig{CertFile: certPath, AdvertisedCAFile: config.SystemRootsCA},
			want: "",
		},
		{
			name: "a path advertises that file verbatim",
			tls:  config.TunnelTLSConfig{CertFile: certPath, AdvertisedCAFile: advertisedPath},
			want: string(advertisedPEM),
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := advertisedTunnelCA(tc.tls)
			if err != nil {
				t.Fatalf("advertisedTunnelCA() error = %v, want nil", err)
			}
			if got != tc.want {
				t.Errorf("advertisedTunnelCA() = %q, want %q", got, tc.want)
			}
		})
	}
}

// TestAdvertisedTunnelCAReadErrors pins the two read-failure paths: the unset
// branch reading a missing listener cert, and the path branch reading a missing
// advertised CA file. Both must surface an error rather than an empty bundle.
func TestAdvertisedTunnelCAReadErrors(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "does-not-exist.pem")

	tests := []struct {
		name string
		tls  config.TunnelTLSConfig
	}{
		{
			name: "unset branch fails when the listener cert is unreadable",
			tls:  config.TunnelTLSConfig{CertFile: missing},
		},
		{
			name: "path branch fails when the advertised CA is unreadable",
			tls:  config.TunnelTLSConfig{CertFile: missing, AdvertisedCAFile: missing},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := advertisedTunnelCA(tc.tls)
			if err == nil {
				t.Fatalf("advertisedTunnelCA() error = nil, want a read error (got %q)", got)
			}
			if got != "" {
				t.Errorf("advertisedTunnelCA() = %q on error, want empty string", got)
			}
		})
	}
}

// TestBuildTunnelTLS pins the tunnel listener's server-cert load: a valid pair
// yields a TLS 1.3 config carrying exactly one certificate; a missing pair is a
// load error.
func TestBuildTunnelTLS(t *testing.T) {
	dir := t.TempDir()
	certPath, keyPath := writeSelfSignedCertKey(t, dir)

	cfg, err := buildTunnelTLS(config.TunnelTLSConfig{CertFile: certPath, KeyFile: keyPath})
	if err != nil {
		t.Fatalf("buildTunnelTLS() error = %v, want nil", err)
	}
	if len(cfg.Certificates) != 1 {
		t.Errorf("buildTunnelTLS() Certificates = %d, want 1", len(cfg.Certificates))
	}
	if cfg.MinVersion != tls.VersionTLS13 {
		t.Errorf("buildTunnelTLS() MinVersion = %x, want %x", cfg.MinVersion, tls.VersionTLS13)
	}
}

// TestBuildTunnelTLSLoadFailure pins the load-failure error path.
func TestBuildTunnelTLSLoadFailure(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "nope")
	if _, err := buildTunnelTLS(config.TunnelTLSConfig{CertFile: missing, KeyFile: missing}); err == nil {
		t.Fatal("buildTunnelTLS() error = nil, want a load error")
	}
}

// TestDialGate pins the control-plane dial's CA-file/insecure gate (RELAY.md
// Section 13). grpc.NewClient is lazy, so a returned conn means the credentials
// branch was selected without a live dial; the error cases pin the CA read and
// the empty-bundle rejection.
func TestDialGate(t *testing.T) {
	dir := t.TempDir()
	certPath, _ := writeSelfSignedCertKey(t, dir)

	emptyBundle := filepath.Join(dir, "empty.pem")
	if err := os.WriteFile(emptyBundle, []byte("not a pem"), 0o600); err != nil {
		t.Fatalf("write empty bundle: %v", err)
	}
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
		conn, err := dial(config.APIConfig{GRPCEndpoint: "127.0.0.1:1", TLS: config.TLSConfig{CAFile: certPath}}, logger)
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

	t.Run("a CA file with no usable certs is a fatal error", func(t *testing.T) {
		if _, err := dial(config.APIConfig{GRPCEndpoint: "127.0.0.1:1", TLS: config.TLSConfig{CAFile: emptyBundle}}, logger); err == nil {
			t.Fatal("dial() error = nil, want a no-usable-certificates error")
		}
	})
}
