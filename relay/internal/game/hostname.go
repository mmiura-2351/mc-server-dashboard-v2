package game

import "strings"

// MatchSlug normalizes a handshake server_address and, if it names a host under
// baseDomain, returns the single-label slug (RELAY.md Section 3). Normalization:
// lowercase, strip a trailing dot, strip Forge's "\0FML…\0" marker suffix. The
// result must be exactly "<slug>.<baseDomain>" with a single non-empty label
// before the domain; a raw IP, an unknown domain, or a multi-label prefix
// returns ok=false (the caller drops it silently).
//
// baseDomain is matched case-insensitively and may carry a leading dot or not.
func MatchSlug(serverAddress, baseDomain string) (slug string, ok bool) {
	host := normalizeHost(serverAddress)
	base := strings.ToLower(strings.TrimSuffix(strings.TrimPrefix(baseDomain, "."), "."))
	if host == "" || base == "" {
		return "", false
	}

	suffix := "." + base
	if !strings.HasSuffix(host, suffix) {
		return "", false
	}
	label := strings.TrimSuffix(host, suffix)
	// Exactly one DNS label before the base domain, and non-empty.
	if label == "" || strings.Contains(label, ".") {
		return "", false
	}
	return label, true
}

// normalizeHost lowercases serverAddress, strips a trailing dot, and removes
// Forge's "\0FML…\0" suffix marker (RELAY.md Section 3). Forge appends
// "\0FML\0", "\0FML2\0", or "\0FML3\0" (and a token list) to the address; the
// real hostname is everything before the first NUL.
func normalizeHost(serverAddress string) string {
	host := serverAddress
	if i := strings.IndexByte(host, 0); i >= 0 {
		host = host[:i]
	}
	host = strings.ToLower(host)
	host = strings.TrimSuffix(host, ".")
	return host
}
