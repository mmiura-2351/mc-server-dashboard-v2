#!/usr/bin/env bash
#
# test_shell_pipefail.sh: repo-wide guard -- no branch is decided by a pipeline
# whose reader can leave early (issues #2447, #2465).
#
# The defect. A quiet grep stops reading at its first match and exits, so a
# producer that has not finished writing takes SIGPIPE -- and `set -o pipefail`
# promotes that 141 to the pipeline's status. The condition then reports the
# OPPOSITE of what the data says, silently: a db-data volume that DOES exist is
# read as absent and the upgrade exits "nothing to do" (#2447); a hook banner
# that IS present is reported missing and a green pre-push turns red (#2344,
# measured at this repo's own dirty-tree assertion: 5 misses in 20000, exit
# status 141).
#
# Why a source check. The defect cannot be pinned any other way: an assertion
# that fails once in a few thousand runs passes any test that runs it a handful
# of times. What IS deterministic is the shape -- feeding a quiet grep from a
# pipe leaves a writer that can be killed mid-write, and feeding it a file or a
# here-string leaves none. So the shape is what is asserted, and the fix for a
# hit is to remove the pipe, never to retry the assertion.
#
# Why the ban is by shape rather than by measured rate. How often a site loses
# the race depends on its producer, and the safe-looking ones are safe only by
# an implementation detail of whatever tool is on the other end today: a
# producer that writes in several chunks loses whenever bytes remain after the
# match (`printf`, 28 in 60000 per #2464), while one that emits everything in a
# single write cannot lose at all (`tail -n 5` on a regular file, 0 in 32000).
# The second number is a property of today's coreutils, not of the code, and an
# edit that splices in a filter reopens the window with nothing to say it did.
#
# Why every script is scanned rather than only the ones that set pipefail. Two
# earlier versions of this guard filtered first -- by the word "pipefail"
# appearing in the file, then by a `set` command plus a worklist that followed
# `source` lines to pick up libraries like pg_cluster_lib.sh, which sets no
# options of its own. Both filters had the same defect one level down: coverage
# depended on how something was spelled. The first dropped a file when a comment
# was reworded; the second dropped one when a source line was written
# `. "$(dirname "$0")/lib.sh"` instead of `. "$script_dir/lib.sh"`. Scanning
# unconditionally costs a few milliseconds per file and deletes that whole
# silent-shrink class rather than patching instances of it. A script that does
# not set pipefail is not exposed, so a hit in one would be a false positive --
# there are none in this repo, and the shape is worth avoiding there anyway,
# since `set -o pipefail` is one edit away.
#
# There is deliberately no "is the scan wide enough" assertion. With the filter
# gone, the only way a pipefail-running file escapes is by not being a shell
# script at all -- a Dockerfile `SHELL ["/bin/bash", "-o", "pipefail", "-c"]` or
# a Makefile `.SHELLFLAGS`. Neither exists in this repo, so such a check could
# not fail today, and a check that cannot fail is worse than none: it reports an
# all-clear nobody has verified.
#
# Scope note (#2465): `| head -n 1` is the same early-exiting-reader family and
# was swept for, but every instance in this repo feeds `head` from a grep or sed
# whose whole output fits one buffered write, so the write has completed before
# `head` can exit -- 0 failures in 20000 for both a 1-line and a 6-line producer.
# Those sites also assign rather than branch, so an inversion cannot go silent.
# They are left alone and this guard does not ban them.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

# `git ls-files` is the file selection: it is the one list that cannot drift out
# of sync with what is actually in the repo. Globbing directories instead needs
# an exclusion for every build and vendor tree (.git, node_modules, .venv, dist,
# .bin) plus .claude/worktrees/, which holds full checkouts of other branches --
# and it still goes blind to a tracked script that lands somewhere the globs
# never named. Drop GIT_* first: pre-push exports GIT_DIR pointing at the real
# repository, which would redirect the listing away from this worktree.
unset "${!GIT_@}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

pass=0
fail=0

ok()        { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

echo "=== pipefail pipeline-shape tests ==="

# Two ways of writing the shape have to be caught or the guard is decoration:
#
#   * the pipe broken across lines, which is how the longer pipelines in
#     pg_major_upgrade.sh and run_relay_e2e.sh are already written -- so it is
#     the form a future edit is MOST likely to use, and a single-line regex is
#     blind to it;
#   * the quiet flag anywhere in the option list and in any spelling -- bundled
#     in either order, given separately, or long (--quiet / --silent). It is the
#     early exit that opens the window, not the position of the letter.
#
# Hence awk over a line regex: `cont` carries "the previous line ended in a
# pipe" so the two-line form is one state, and `quiet_grep` matches a grep whose
# options contain a quiet flag however it is spelled.
{
	offenders=""
	scanned=0
	quiet_grep='grep([[:space:]]+-[^[:space:]]+)*[[:space:]]+(-[[:alpha:]]*q[[:alpha:]]*|--quiet|--silent)'
	while IFS= read -r rel; do
		f="$ROOT/$rel"
		[ -f "$f" ] || continue
		# Shell scripts and workflows by extension. Anything else is sniffed
		# only when it has no extension at all, which is what the hooks are:
		# .githooks/pre-push and friends carry a shebang and no suffix.
		base="${rel##*/}"
		case "$base" in
			*.sh | *.bash | *.yml | *.yaml) ;;
			*.*) continue ;;
			*)
				# A redirect, not a pipe: reading a file leaves no writer to kill.
				shebang=""
				read -r shebang < "$f" 2> /dev/null || true
				case "$shebang" in
					'#!'*sh*) ;;
					*) continue ;;
				esac
				;;
		esac
		scanned=$((scanned + 1))
		while IFS= read -r line_no; do
			offenders="$offenders ${rel}:${line_no}"
		done < <(awk -v qg="$quiet_grep" '
			{
				if ($0 ~ "\\|[[:space:]]*" qg) print NR
				else if (cont && $0 ~ "^[[:space:]]*" qg) print NR
				cont = ($0 ~ /\|[[:space:]]*$/)
			}
		' "$f" || true)
	done < <(git -C "$ROOT" ls-files)
	if [ -z "$offenders" ]; then
		ok "no branch is decided by a quiet grep reading from a pipe ($scanned files)"
	else
		fail_test "a quiet grep is fed from a pipe -- SIGPIPE under pipefail (#2447) at:$offenders"
	fi
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
