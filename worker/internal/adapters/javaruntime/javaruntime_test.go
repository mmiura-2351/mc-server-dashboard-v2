package javaruntime

import (
	"testing"
)

func TestMajorsForMapping(t *testing.T) {
	cases := []struct {
		version string
		want    int // most-preferred Java major
	}{
		{"1.7.9", 7},
		{"1.7.2", 7},
		{"1.7.10", 8},
		{"1.12.2", 8},
		{"1.16.5", 8},
		{"1.17", 16},
		{"1.17.1", 16},
		{"1.18", 17},
		{"1.20.4", 17},
		{"1.20.5", 21},
		{"1.21", 21},
		{"1.21.11", 21},
		{"26.0", 25},
		{"27.3", 25},
	}

	for _, tc := range cases {
		t.Run(tc.version, func(t *testing.T) {
			majors, err := MajorsFor(tc.version)
			if err != nil {
				t.Fatalf("MajorsFor(%q) error: %v", tc.version, err)
			}
			if len(majors) == 0 || majors[0] != tc.want {
				t.Fatalf("MajorsFor(%q) = %v, want most-preferred Java %d", tc.version, majors, tc.want)
			}
		})
	}
}

// The 1.7.10-1.16.5 bracket prefers Java 8 but falls back to Java 11; it is the
// only bracket with a fallback (JAVA_COMPATIBILITY.md).
func TestMajorsForLegacyBracketHasJava11Fallback(t *testing.T) {
	majors, err := MajorsFor("1.12.2")
	if err != nil {
		t.Fatalf("MajorsFor error: %v", err)
	}
	want := []int{8, 11}
	if len(majors) != len(want) {
		t.Fatalf("MajorsFor(1.12.2) = %v, want %v", majors, want)
	}
	for i := range want {
		if majors[i] != want[i] {
			t.Fatalf("MajorsFor(1.12.2) = %v, want %v", majors, want)
		}
	}
}

func TestMajorsForUnparseableVersion(t *testing.T) {
	_, err := MajorsFor("not-a-version")
	if err == nil {
		t.Fatalf("MajorsFor(garbage) expected error")
	}
}
