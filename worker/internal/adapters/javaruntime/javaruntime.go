// Package javaruntime maps a Minecraft version to the required Java major
// version(s) via the legacy compatibility table (FR-EXE-5, ARCHITECTURE.md
// Section 7.3). The container driver resolves a base image by this bracket logic.
//
// The version→Java mapping follows the legacy reference
// (https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/JAVA_COMPATIBILITY.md).
package javaruntime

import (
	"fmt"
	"strconv"
	"strings"
)

// MajorsFor returns the acceptable Java major versions for a Minecraft version,
// most-preferred first. The legacy mapping pins a single Java major per bracket
// except 1.7.10-1.16.5, which prefers 8 with an 11 fallback. It is exported so
// the container driver can resolve a base image by this bracket logic.
func MajorsFor(mcVersion string) ([]int, error) {
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
		// Policy: a Minecraft version newer than every bracket in this table
		// selects the newest configured runtime. New MC releases generally require
		// the latest Java, so until the table is extended with the next published
		// bracket, the safest default is the most recent Java we know about (the
		// current newest bracket targets Java 25). The table is the authoritative
		// list and is extended as Mojang publishes new Java requirements.
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
