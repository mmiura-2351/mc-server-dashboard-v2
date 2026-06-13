// Package netutil provides small network-address helpers shared across the
// relay's listener packages.
package netutil

import "net"

// HostOf extracts the IP (without port) from a remote address.
func HostOf(addr net.Addr) string {
	host, _, err := net.SplitHostPort(addr.String())
	if err != nil {
		return addr.String()
	}
	return host
}
