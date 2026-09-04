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
#   1. A version bump reinstalls -- with the binary present, asking for it at a
#      version that has never been installed here produces a `go install` of
#      *that* version. This is the half that regressed: it fails on the bare
#      file target with "is up to date".
#   2. A first run installs -- with no binary at all, the install still runs
#      (the `make bootstrap` path in a fresh worktree).
#   3. Steady state does nothing -- binary present and its stamp current, no
#      install. Without this, "always reinstall" would pass assertions 1 and 2
#      while paying a `go install` on every lint.
#
# Hermetic by construction: every run is `make -n` (dry run -- nothing is
# executed, nothing is installed, no network) against a temp path substituted
# for $(GOLANGCI) via a command-line override, so nothing in the developer's
# real worker/.bin is written. It is read: assertions 1 and 2 leave
# $(GOLANGCI_STAMP) at its default, which resolves inside the real worker/.bin,
# and make stats that path to decide whether the install rule is out of date --
# which is the point of assertion 1, since the stamp name is what carries the
# version. Command-line overrides win over the `:=` assignments in the Makefile,
# which is what lets that name be derived from an overridden GOLANGCI_VERSION.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

pass=0
fail=0

ok()        { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "=== golangci-lint version-pin tests ==="

# ---------------------------------------------------------------------------
# 1. A version bump forces the reinstall.
{
	bin="$tmp/bumped-golangci-lint"
	: > "$bin"

	recipe="$(cd "$ROOT" && make -n "$bin" GOLANGCI="$bin" \
		GOLANGCI_VERSION=v0.0.0-test 2>&1)"
	case "$recipe" in
		*"golangci-lint@v0.0.0-test"*)
			ok "a bumped GOLANGCI_VERSION reinstalls over an existing binary" ;;
		*)
			fail_test "a bumped GOLANGCI_VERSION did not reinstall (make said: $(echo "$recipe" | tr '\n' ' '))" ;;
	esac
}

# ---------------------------------------------------------------------------
# 2. A first run installs (fresh worktree / `make bootstrap`).
{
	bin="$tmp/absent-golangci-lint"

	recipe="$(cd "$ROOT" && make -n "$bin" GOLANGCI="$bin" 2>&1)"
	case "$recipe" in
		*"go install"*golangci-lint*)
			ok "a missing binary is installed" ;;
		*)
			fail_test "a missing binary was not installed (make said: $(echo "$recipe" | tr '\n' ' '))" ;;
	esac
}

# ---------------------------------------------------------------------------
# 3. Binary present at the pinned version: nothing to do.
{
	bin="$tmp/current-golangci-lint"
	stamp="$tmp/current-stamp"
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
