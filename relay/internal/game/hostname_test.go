package game

import "testing"

func TestMatchSlug(t *testing.T) {
	const base = "mc.example.com"
	cases := []struct {
		name string
		addr string
		want string
		ok   bool
	}{
		{"simple", "amber-falcon-42.mc.example.com", "amber-falcon-42", true},
		{"uppercase normalized", "AMBER.MC.EXAMPLE.COM", "amber", true},
		{"trailing dot stripped", "amber.mc.example.com.", "amber", true},
		{"forge FML marker stripped", "amber.mc.example.com\x00FML\x00", "amber", true},
		{"forge FML3 marker stripped", "amber.mc.example.com\x00FML3\x00\x00", "amber", true},
		{"raw ip rejected", "203.0.113.7", "", false},
		{"unknown domain rejected", "amber.other.example.com", "", false},
		{"multi-label prefix rejected", "a.b.mc.example.com", "", false},
		{"base domain itself rejected", "mc.example.com", "", false},
		{"empty label rejected", ".mc.example.com", "", false},
		{"empty address rejected", "", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := MatchSlug(tc.addr, base)
			if ok != tc.ok || got != tc.want {
				t.Errorf("MatchSlug(%q) = (%q, %v), want (%q, %v)", tc.addr, got, ok, tc.want, tc.ok)
			}
		})
	}
}

func TestMatchSlugEmptyBaseDomain(t *testing.T) {
	// Before the relay learns base_domain (Register not yet succeeded), nothing
	// matches and every connection is dropped.
	if _, ok := MatchSlug("amber.mc.example.com", ""); ok {
		t.Error("empty base_domain must match nothing")
	}
}
