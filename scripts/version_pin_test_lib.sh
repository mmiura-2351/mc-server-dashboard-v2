#!/usr/bin/env bash
#
# version_pin_test_lib.sh: shared hermetic probes for the version-pin suites.
#
# The two callers remain subject-specific, self-contained suites: each owns its
# fixtures, assertions and PASS/FAIL reporting. This file only centralizes the
# Makefile-variable probe and the common stamp facts, so a hermeticity fix cannot
# silently reach one suite but miss the other.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Ask make what a variable expands to, under the `VAR=value` overrides passed
# after the variable name. `--eval` defines a throwaway target *before* the
# makefile is read -- the variable is still empty at that point -- and what
# makes the probe work is that a recipe body is expanded only when the recipe
# runs, by which time the makefile has defined it. So the answer is make's own
# expansion and this script never reimplements the derivation it is pinning.
# --no-print-directory: `make scripts-test` runs these suites with MAKELEVEL
# exported, and a make that sees MAKELEVEL > 0 counts itself a sub-make and
# turns on -w, which would put "Entering directory" on stdout.
mk() {
	local var="$1"
	shift
	(cd "$ROOT" && make --no-print-directory \
		--eval="mcsd-probe:;@echo \$($var)" mcsd-probe "$@")
}

# `$(<stamp var>)` for a given `$(<version var>)`, rebased into a temporary
# directory: make's name, the caller's directory.
stamp_in() {
	local dir="$1" stamp_var="$2" version_var="$3" version="$4"
	echo "$dir/$(basename "$(mk "$stamp_var" "$version_var=$version")")"
}

# Every tool that shares the worker/.bin stamp mechanism, as
# <binary var>:<stamp var>:<version var>. The protoc cleanup assertion seeds
# one old stamp for every entry, so a cleanup glob cannot start deleting another
# tool's stamp unnoticed.
VERSION_PIN_TOOLS=(
	"GOLANGCI:GOLANGCI_STAMP:GOLANGCI_VERSION"
	"PROTOC_GEN_GO:PROTOC_GEN_GO_STAMP:PROTOC_GEN_GO_VERSION"
	"PROTOC_GEN_GO_GRPC:PROTOC_GEN_GO_GRPC_STAMP:PROTOC_GEN_GO_GRPC_VERSION"
)

# assert_stamp_directory <label> <binary|plugin> <binary var> <stamp var>
#
# Callers supply `ok` and `fail_test`, preserving their own counters and loud
# output while sharing the string-only directory comparison. An empty label is
# the golangci suite's unprefixed form.
assert_stamp_directory() {
	local label="$1" kind="$2" bin_var="$3" stamp_var="$4"
	local prefix="" stamp_dir bin_dir

	[ -n "$label" ] && prefix="$label: "
	stamp_dir="$(dirname "$(mk "$stamp_var")")"
	bin_dir="$(dirname "$(mk "$bin_var")")"
	if [ "$stamp_dir" = "$bin_dir" ]; then
		ok "${prefix}the stamp sits in the ${kind}'s directory"
	else
		fail_test "${prefix}the stamp is not swept with the $kind ($stamp_var is in $stamp_dir, $bin_var in $bin_dir)"
	fi
}
