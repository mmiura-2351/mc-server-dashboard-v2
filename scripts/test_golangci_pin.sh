#!/usr/bin/env bash
#
# test_golangci_pin.sh: bumping GOLANGCI_VERSION reinstalls golangci-lint
# (issue #2903).
#
# The defect. `$(GOLANGCI)` (worker/.bin/golangci-lint) was a bare file target:
# make only asked whether that path existed, never which version it held. So a
# bump of GOLANGCI_VERSION had no effect on any checkout that already had the
# binary -- `make worker-lint`, `make relay-lint` and the pre-push `make check`
# kept running the old linter, silently, and the local gate disagreed with CI
# (which keys its lint cache on the resolved version). The fix gives the binary
# a version-stamped prerequisite, so a bump renames the prerequisite out of
# existence and forces the reinstall.
#
# What is asserted:
#
#   1. The stamp name carries the pin -- $(GOLANGCI_STAMP) resolves to a name
#      that embeds whatever GOLANGCI_VERSION says. That derivation *is* the fix
#      (#2928): a stamp whose name stops varying with the version is a fixed
#      name again, which is the #2903 defect restored. The probe uses a version
#      that appears nowhere in the Makefile, so a name that hardcoded today's
#      pin instead of deriving it from the variable fails here too.
#   2. A version bump reinstalls -- with the binary present and the *previous*
#      version's stamp beside it, asking for the binary at the current pin
#      produces a `go install` of that pin. This is the half that regressed: it
#      fails on the bare file target with "is up to date".
#   3. A first run installs -- with no binary at all, the install still runs
#      (the `make bootstrap` path in a fresh worktree).
#   4. Steady state does nothing -- binary present and its stamp current, no
#      install. Without this, "always reinstall" would pass assertions 2 and 3
#      while paying a `go install` on every lint.
#
# Hermetic by construction: every run is `make -n` (dry run -- nothing is
# executed, nothing is installed, no network) against temp paths substituted for
# $(GOLANGCI) and $(GOLANGCI_STAMP), so the developer's real worker/.bin is
# neither written nor read, and no result depends on whether a lint has already
# run in this checkout.
#
# Relocating the stamp does not hide the derivation under test, because the
# stamp's *name* is still make's own: `mk` asks make what $(GOLANGCI_STAMP)
# expands to under a given GOLANGCI_VERSION, and only the directory of that
# answer is replaced. So if the name stopped carrying the version, assertion 2's
# "previous version" stamp would land on the same path as the current one, the
# prerequisite would already be satisfied, and no reinstall would be dry-run --
# red, whatever worker/.bin happens to hold.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

pass=0
fail=0

ok()        { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

# Ask make what a variable expands to, under the `VAR=value` overrides passed
# after the variable name. `--eval` appends a throwaway target to the makefile
# make has just read, so the answer is make's own expansion and this script
# never reimplements the derivation it is pinning. --no-print-directory:
# `make scripts-test` runs this script with MAKEFLAGS exported, which makes the
# call below a sub-make and would otherwise put "Entering directory" on stdout.
mk() {
	local var="$1"
	shift
	(cd "$ROOT" && make --no-print-directory \
		--eval="mcsd-probe:;@echo \$($var)" mcsd-probe "$@")
}

# $(GOLANGCI_STAMP) for a given GOLANGCI_VERSION, rebased into a temp directory:
# make's name, our directory.
stamp_in() {
	echo "$1/$(basename "$(mk GOLANGCI_STAMP GOLANGCI_VERSION="$2")")"
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

version="$(mk GOLANGCI_VERSION)"

echo "=== golangci-lint version-pin tests ==="

# ---------------------------------------------------------------------------
# 1. The stamp name carries the pinned version.
{
	stamp="$(mk GOLANGCI_STAMP GOLANGCI_VERSION=v0.0.0-test)"
	case "$stamp" in
		*v0.0.0-test*)
			ok "the stamp name carries the pinned version" ;;
		*)
			fail_test "the stamp name does not carry the pinned version (GOLANGCI_STAMP resolved to: $stamp)" ;;
	esac
}

# ---------------------------------------------------------------------------
# 2. A version bump forces the reinstall: the previous version's stamp does not
#    satisfy the current pin.
{
	dir="$tmp/bumped"
	mkdir -p "$dir"
	bin="$dir/golangci-lint"
	: > "$(stamp_in "$dir" v0.0.0-previous)"
	: > "$bin"

	recipe="$(cd "$ROOT" && make -n "$bin" GOLANGCI="$bin" \
		GOLANGCI_STAMP="$(stamp_in "$dir" "$version")" 2>&1)"
	case "$recipe" in
		*"golangci-lint@$version"*)
			ok "a bumped GOLANGCI_VERSION reinstalls over an existing binary" ;;
		*)
			fail_test "a bumped GOLANGCI_VERSION did not reinstall (make said: $(echo "$recipe" | tr '\n' ' '))" ;;
	esac
}

# ---------------------------------------------------------------------------
# 3. A first run installs (fresh worktree / `make bootstrap`).
{
	dir="$tmp/absent"
	mkdir -p "$dir"
	bin="$dir/golangci-lint"

	recipe="$(cd "$ROOT" && make -n "$bin" GOLANGCI="$bin" \
		GOLANGCI_STAMP="$(stamp_in "$dir" "$version")" 2>&1)"
	case "$recipe" in
		*"go install"*golangci-lint*)
			ok "a missing binary is installed" ;;
		*)
			fail_test "a missing binary was not installed (make said: $(echo "$recipe" | tr '\n' ' '))" ;;
	esac
}

# ---------------------------------------------------------------------------
# 4. Binary present at the pinned version: nothing to do.
{
	dir="$tmp/current"
	mkdir -p "$dir"
	bin="$dir/golangci-lint"
	stamp="$(stamp_in "$dir" "$version")"
	: > "$stamp"
	: > "$bin"

	recipe="$(cd "$ROOT" && make -n "$bin" GOLANGCI="$bin" \
		GOLANGCI_STAMP="$stamp" 2>&1)"
	case "$recipe" in
		*"go install"*)
			fail_test "the pinned version reinstalled needlessly (make said: $(echo "$recipe" | tr '\n' ' '))" ;;
		*)
			ok "an up-to-date binary is left alone" ;;
	esac
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
