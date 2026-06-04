package javaruntime

import (
	"errors"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// runtimes covers every Java major the mapping can request, so a selection test
// asserts on the version→major→path resolution end to end.
func allRuntimes() map[int]string {
	return map[int]string{
		7:  "/jvm/7/bin/java",
		8:  "/jvm/8/bin/java",
		11: "/jvm/11/bin/java",
		16: "/jvm/16/bin/java",
		17: "/jvm/17/bin/java",
		21: "/jvm/21/bin/java",
		25: "/jvm/25/bin/java",
	}
}

func TestSelectMapping(t *testing.T) {
	cases := []struct {
		version string
		want    int // expected Java major
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

	sel := New(allRuntimes())
	for _, tc := range cases {
		t.Run(tc.version, func(t *testing.T) {
			path, err := sel.Select(tc.version)
			if err != nil {
				t.Fatalf("Select(%q) error: %v", tc.version, err)
			}
			want := allRuntimes()[tc.want]
			if path != want {
				t.Fatalf("Select(%q) = %q, want %q (Java %d)", tc.version, path, want, tc.want)
			}
		})
	}
}

// The 1.7.10-1.16.5 bracket prefers Java 8 but falls back to Java 11 when 8 is
// not installed; it is the only bracket with a fallback (JAVA_COMPATIBILITY.md).
func TestSelectLegacyFallsBackToJava11(t *testing.T) {
	sel := New(map[int]string{11: "/jvm/11/bin/java"})
	path, err := sel.Select("1.12.2")
	if err != nil {
		t.Fatalf("Select fallback error: %v", err)
	}
	if path != "/jvm/11/bin/java" {
		t.Fatalf("Select(1.12.2) = %q, want Java 11 fallback", path)
	}
}

func TestSelectNoRuntimeConfigured(t *testing.T) {
	sel := New(map[int]string{8: "/jvm/8/bin/java"})
	_, err := sel.Select("1.20.4") // needs Java 17, only 8 installed
	if !errors.Is(err, execution.ErrNoRuntime) {
		t.Fatalf("Select error = %v, want ErrNoRuntime", err)
	}
}

func TestSelectUnparseableVersion(t *testing.T) {
	sel := New(allRuntimes())
	_, err := sel.Select("not-a-version")
	if err == nil {
		t.Fatalf("Select(garbage) expected error")
	}
}
