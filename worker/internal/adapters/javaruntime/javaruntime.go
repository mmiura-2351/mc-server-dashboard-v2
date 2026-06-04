// Package javaruntime implements the execution.JavaRuntimeSelector Port: it maps
// a Minecraft version to the required Java major version, then resolves that to a
// configured local runtime path (FR-EXE-5, ARCHITECTURE.md Section 7.3).
//
// The version→Java mapping follows the legacy reference
// (https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/JAVA_COMPATIBILITY.md);
// the runtime paths come from worker.java.runtimes config (CONFIGURATION.md
// Section 6.3).
package javaruntime

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// Selector resolves Minecraft versions to installed Java runtime paths.
type Selector struct {
	// runtimes maps a Java major version to the java binary path for it.
	runtimes map[int]string
}

// New builds a Selector over the configured Java-major→path runtimes map.
func New(runtimes map[int]string) *Selector {
	return &Selector{runtimes: runtimes}
}

// Select returns the java binary path for mcVersion. It picks the required Java
// major from the legacy mapping, then resolves the configured path; the
// 1.7.10-1.16.5 bracket prefers Java 8 and falls back to Java 11 when 8 is not
// installed (the only bracket with a fallback). It returns execution.ErrNoRuntime
// when no configured runtime satisfies the version, or a parse error when
// mcVersion is not a recognizable version string.
func (s *Selector) Select(mcVersion string) (string, error) {
	majors, err := javaMajorsFor(mcVersion)
	if err != nil {
		return "", err
	}
	for _, major := range majors {
		if path, ok := s.runtimes[major]; ok {
			return path, nil
		}
	}
	return "", fmt.Errorf("%w: Minecraft %s needs Java %v, none configured", execution.ErrNoRuntime, mcVersion, majors)
}

// javaMajorsFor returns the acceptable Java major versions for a Minecraft
// version, most-preferred first. The legacy mapping pins a single Java major per
// bracket except 1.7.10-1.16.5, which prefers 8 with an 11 fallback.
func javaMajorsFor(mcVersion string) ([]int, error) {
	v, err := parseVersion(mcVersion)
	if err != nil {
		return nil, err
	}
	switch {
	case v.atMost(1, 7, 9):
		return []int{7}, nil
	case v.atMost(1, 16, 5):
		return []int{8, 11}, nil
	case v.atMost(1, 17, 1):
		return []int{16}, nil
	case v.atMost(1, 20, 4):
		return []int{17}, nil
	case v.atMost(1, 21, 11):
		return []int{21}, nil
	default:
		// 26.x and newer (year-based versioning): server.jar targets Java 25.
		return []int{25}, nil
	}
}

// version is a parsed major.minor.patch Minecraft version; missing components are
// zero (e.g. "1.17" → {1,17,0}).
type version struct {
	major, minor, patch int
}

// atMost reports whether v <= the given major.minor.patch, comparing
// lexicographically.
func (v version) atMost(major, minor, patch int) bool {
	if v.major != major {
		return v.major < major
	}
	if v.minor != minor {
		return v.minor < minor
	}
	return v.patch <= patch
}

// parseVersion parses up to three dot-separated numeric components. A version
// with no parseable leading number is an error.
func parseVersion(s string) (version, error) {
	parts := strings.Split(strings.TrimSpace(s), ".")
	nums := make([]int, 3)
	for i := 0; i < 3 && i < len(parts); i++ {
		n, err := strconv.Atoi(parts[i])
		if err != nil {
			return version{}, fmt.Errorf("javaruntime: unparseable Minecraft version %q: %w", s, err)
		}
		nums[i] = n
	}
	return version{major: nums[0], minor: nums[1], patch: nums[2]}, nil
}
