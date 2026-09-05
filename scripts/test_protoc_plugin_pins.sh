#!/usr/bin/env bash
#
# test_protoc_plugin_pins.sh: bumping PROTOC_GEN_GO_VERSION or
# PROTOC_GEN_GO_GRPC_VERSION reinstalls that plugin (issue #2927).
#
# The defect. `$(PROTOC_GEN_GO)` and `$(PROTOC_GEN_GO_GRPC)` --
# worker/.bin/protoc-gen-go and worker/.bin/protoc-gen-go-grpc -- were bare file
# targets on fixed, unversioned paths: make only asked whether the path existed,
# never which version it held. `proto-gen` depends on both, so bumping either
# pin had no effect on any checkout that already had the plugin.
#
# Unlike the golangci-lint instance of the same defect (#2903) this one is not
# silent: the generated stubs carry the plugin version in their header, so a
# stale plugin surfaces as a `proto-check` drift failure. It is merely
# confusing -- the failure reads as "the committed stubs are stale", its
# direction depends on whether whoever regenerated last had the new plugin or
# the old one, and a regeneration with a stale plugin walks the version header
# backwards past a gate that accepts it.
#
# The fix is the one #2903 established for golangci-lint: a version-named stamp
# file as a prerequisite, so a bump renames the prerequisite out of existence
# and forces the reinstall, with the plugin's own path left fixed so every
# hard-coded reference to it keeps working.
#
# What is asserted, for each plugin:
#
#   1. The stamp name carries the pin -- the stamp variable resolves to a name
#      that embeds whatever the version variable says. That derivation *is* the
#      fix: a stamp whose name stops varying with the version is a fixed name
#      again, which is the defect restored. The probe uses a version that
#      appears nowhere in the Makefile, so a name that hardcoded today's pin
#      instead of deriving it from the variable fails here too.
#   2. A version bump reinstalls -- with the plugin present and the *previous*
#      version's stamp beside it, asking for the plugin at a version that has
#      never been installed here produces a `go install` of *that* version.
#      This is the half that regressed: on a bare file target make answers "is
#      up to date" and installs nothing.
#   3. A first run installs -- with no plugin at all, the install still runs
#      (the fresh-worktree `make proto-gen` path).
#   4. Steady state does nothing -- plugin present and its stamp current, no
#      install. Without this, "always reinstall" would pass 2 and 3 while paying
#      a `go install` on every `proto-gen`.
#   5. The stamp cleanup is per tool -- worker/.bin now holds stamps for three
#      tools, and `protoc-gen-go` is a *prefix* of `protoc-gen-go-grpc`, so a
#      cleanup glob copied over as a bare `<tool>-*.stamp` would sweep the
#      sibling's stamp away too, dragging a reinstall of that sibling along with
#      every bump of this one. Installing a stamp into a directory holding all
#      three tools' stamps must remove this tool's superseded stamp and nothing
#      else.
#
# Hermetic by construction. Assertions 1-4 are `make -n` runs (dry run --
# nothing executed, nothing installed, no network) against temp paths
# substituted for both the plugin and its stamp, so the developer's real
# worker/.bin is neither written nor read, and no result depends on whether a
# `proto-gen` has already run in this checkout. Assertion 5 is the one place
# this suite lets make execute a recipe, because deletion is the behavior it is
# about: the stamp rule has no prerequisites, names no tool path, and derives
# every path it writes from `$(dir $@)` -- which that run overrides into a temp
# directory. The `mk` probe below executes only its own `echo` of a variable,
# which likewise reads and writes nothing.
#
# Relocating the stamps does not hide the derivation under test, because a
# stamp's *name* is still make's own: `mk` asks make what the stamp variable
# expands to under a given version, and only the directory of that answer is
# replaced.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

pass=0
fail=0

ok()        { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

# Ask make what a variable expands to, under the `VAR=value` overrides passed
# after the variable name. `--eval` defines a throwaway target *before* the
# makefile is read -- the variable is still empty at that point -- and what
# makes the probe work is that a recipe body is expanded only when the recipe
# runs, by which time the makefile has defined it. So the answer is make's own
# expansion and this script never reimplements the derivation it is pinning.
# --no-print-directory: `make scripts-test` runs this script with MAKELEVEL
# exported, and a make that sees MAKELEVEL > 0 counts itself a sub-make and
# turns on -w, which would put "Entering directory" on stdout.
mk() {
	local var="$1"
	shift
	(cd "$ROOT" && make --no-print-directory \
		--eval="mcsd-probe:;@echo \$($var)" mcsd-probe "$@")
}

# A tool's stamp for a given version, rebased into a temp directory: make's
# name, our directory.
stamp_in() {
	local dir="$1" stamp_var="$2" version_var="$3" version="$4"
	echo "$dir/$(basename "$(mk "$stamp_var" "$version_var=$version")")"
}

# Every tool installed through the stamp mechanism, as <stamp var>:<version
# var>. Assertion 5 seeds one stamp per entry, so a cleanup that reaches beyond
# its own tool is caught whichever pair of names collides.
STAMP_VARS=(
	"GOLANGCI_STAMP:GOLANGCI_VERSION"
	"PROTOC_GEN_GO_STAMP:PROTOC_GEN_GO_VERSION"
	"PROTOC_GEN_GO_GRPC_STAMP:PROTOC_GEN_GO_GRPC_VERSION"
)

# One stamp per tool, all at the same superseded version.
seed_stamps() {
	local dir="$1" entry
	for entry in "${STAMP_VARS[@]}"; do
		: > "$(stamp_in "$dir" "${entry%%:*}" "${entry##*:}" v0.0.0-old)"
	done
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# assert_plugin <label> <bin var> <stamp var> <version var> <install token>
#
# <install token> is the `<binary>@` fragment of the `go install` line, which
# differs between the two plugins even though one name prefixes the other
# (`protoc-gen-go@` vs `protoc-gen-go-grpc@`), so a match cannot be satisfied by
# the wrong rule.
assert_plugin() {
	local label="$1" bin_var="$2" stamp_var="$3" version_var="$4" token="$5"
	local version bin_name dir bin stamp recipe own_old new entry sibling missing

	version="$(mk "$version_var")"
	bin_name="$(basename "$(mk "$bin_var")")"

	echo "--- $label ---"

	# -----------------------------------------------------------------------
	# 1. The stamp name carries the pinned version.
	stamp="$(mk "$stamp_var" "$version_var=v0.0.0-test")"
	case "$stamp" in
		*v0.0.0-test*)
			ok "$label: the stamp name carries the pinned version" ;;
		*)
			fail_test "$label: the stamp name does not carry the pinned version ($stamp_var resolved to: $stamp)" ;;
	esac

	# -----------------------------------------------------------------------
	# 2. A version bump forces the reinstall: the previous version's stamp does
	#    not satisfy the current pin.
	dir="$tmp/$label-bumped"
	mkdir -p "$dir"
	bin="$dir/$bin_name"
	: > "$(stamp_in "$dir" "$stamp_var" "$version_var" v0.0.0-previous)"
	: > "$bin"

	# Both overrides are load-bearing: the version is what the install line must
	# carry, and the stamp override relocates make's derived name into $dir
	# beside the previous version's stamp.
	recipe="$(cd "$ROOT" && make -n "$bin" "$bin_var=$bin" \
		"$version_var=v0.0.0-bumped" \
		"$stamp_var=$(stamp_in "$dir" "$stamp_var" "$version_var" v0.0.0-bumped)" 2>&1)"
	case "$recipe" in
		*"$token"v0.0.0-bumped*)
			ok "$label: a bumped version reinstalls over an existing plugin" ;;
		*)
			fail_test "$label: a bumped version did not reinstall (make said: $(echo "$recipe" | tr '\n' ' '))" ;;
	esac

	# -----------------------------------------------------------------------
	# 3. A first run installs (fresh worktree / `make proto-gen`).
	dir="$tmp/$label-absent"
	mkdir -p "$dir"
	bin="$dir/$bin_name"

	recipe="$(cd "$ROOT" && make -n "$bin" "$bin_var=$bin" \
		"$stamp_var=$(stamp_in "$dir" "$stamp_var" "$version_var" "$version")" 2>&1)"
	case "$recipe" in
		*"go install"*"$token"*)
			ok "$label: a missing plugin is installed" ;;
		*)
			fail_test "$label: a missing plugin was not installed (make said: $(echo "$recipe" | tr '\n' ' '))" ;;
	esac

	# -----------------------------------------------------------------------
	# 4. Plugin present at the pinned version: nothing to do.
	dir="$tmp/$label-current"
	mkdir -p "$dir"
	bin="$dir/$bin_name"
	stamp="$(stamp_in "$dir" "$stamp_var" "$version_var" "$version")"
	: > "$stamp"
	: > "$bin"

	recipe="$(cd "$ROOT" && make -n "$bin" "$bin_var=$bin" \
		"$stamp_var=$stamp" 2>&1)"
	case "$recipe" in
		*"go install"*)
			fail_test "$label: the pinned version reinstalled needlessly (make said: $(echo "$recipe" | tr '\n' ' '))" ;;
		*)
			ok "$label: an up-to-date plugin is left alone" ;;
	esac

	# -----------------------------------------------------------------------
	# 5. The stamp cleanup removes only this tool's superseded stamps.
	dir="$tmp/$label-cleanup"
	mkdir -p "$dir"
	seed_stamps "$dir"
	own_old="$(stamp_in "$dir" "$stamp_var" "$version_var" v0.0.0-old)"
	new="$(stamp_in "$dir" "$stamp_var" "$version_var" v0.0.0-new)"

	(cd "$ROOT" && make --no-print-directory "$new" "$stamp_var=$new") >/dev/null 2>&1

	missing=""
	for entry in "${STAMP_VARS[@]}"; do
		sibling="$(stamp_in "$dir" "${entry%%:*}" "${entry##*:}" v0.0.0-old)"
		[ "$sibling" = "$own_old" ] && continue
		[ -e "$sibling" ] || missing="$missing $(basename "$sibling")"
	done

	if [ ! -e "$new" ]; then
		fail_test "$label: the stamp for the new version was not created"
	elif [ -e "$own_old" ]; then
		fail_test "$label: the superseded stamp was not removed ($(basename "$own_old"))"
	elif [ -n "$missing" ]; then
		fail_test "$label: the cleanup deleted another tool's stamp:$missing"
	else
		ok "$label: the stamp cleanup removes only this tool's stamps"
	fi
}

echo "=== protoc plugin version-pin tests ==="

assert_plugin protoc-gen-go \
	PROTOC_GEN_GO PROTOC_GEN_GO_STAMP PROTOC_GEN_GO_VERSION "protoc-gen-go@"

assert_plugin protoc-gen-go-grpc \
	PROTOC_GEN_GO_GRPC PROTOC_GEN_GO_GRPC_STAMP PROTOC_GEN_GO_GRPC_VERSION \
	"protoc-gen-go-grpc@"

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
