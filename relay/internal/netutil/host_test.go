package netutil

import (
	"net"
	"testing"
)

func TestHostOf(t *testing.T) {
	t.Run("ip4 with port", func(t *testing.T) {
		addr := addrStr("192.168.1.1:25565")
		if got := HostOf(addr); got != "192.168.1.1" {
			t.Fatalf("got %q, want %q", got, "192.168.1.1")
		}
	})
	t.Run("ip6 with port", func(t *testing.T) {
		addr := addrStr("[::1]:25565")
		if got := HostOf(addr); got != "::1" {
			t.Fatalf("got %q, want %q", got, "::1")
		}
	})
	t.Run("no port fallback", func(t *testing.T) {
		addr := addrStr("192.168.1.1")
		if got := HostOf(addr); got != "192.168.1.1" {
			t.Fatalf("got %q, want %q", got, "192.168.1.1")
		}
	})
}

// addrStr implements net.Addr with a literal string.
type addrStr string

func (a addrStr) Network() string { return "tcp" }
func (a addrStr) String() string  { return string(a) }

// Compile-time check.
var _ net.Addr = addrStr("")
