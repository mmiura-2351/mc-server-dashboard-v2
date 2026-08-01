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
# Scope note (#2465): `| head -n 1` is the same early-exiting-reader family and
# was swept for, but every instance in this repo feeds `head` from a grep or sed
# whose whole output fits one buffered write, so the write has completed before
# `head` can exit -- 0 failures in 20000 for both a 1-line and a 6-line producer.
# Those sites also assign rather than branch, so an inversion cannot go silent.
# They are left alone and this guard does not ban them.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

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
quiet_grep='grep([[:space:]]+-[^[:space:]]+)*[[:space:]]+(-[[:alpha:]]*q[[:alpha:]]*|--quiet|--silent)'

# `set -o pipefail` as a COMMAND, not the word appearing in prose. Selecting on
# the bare word would make coverage depend on comment wording: pg_cluster_lib.sh
# would be scanned only because a comment in it happens to say "pipefail", and
# rewording that comment would drop it out silently.
pipefail_set='^[[:space:]]*set[[:space:]][^#]*pipefail'

# Candidates: every shell script and workflow in the repo. Both workflow
# extensions -- GitHub accepts .yaml as readily as .yml, and a guard that globs
# one of them is a guard the next workflow can walk past.
declare -a candidates=()
for f in "$ROOT"/.githooks/* "$ROOT"/scripts/*.sh \
	"$ROOT"/.github/workflows/*.yml "$ROOT"/.github/workflows/*.yaml; do
	[ -f "$f" ] && candidates+=("$f")
done

# The scan set: candidates that run `set -o pipefail` themselves, then the
# libraries those source, transitively. pg_cluster_lib.sh sets no shell options
# of its own -- it runs under pipefail because pg_major_upgrade.sh and
# deploy_preflight.sh source it, and being sourced is the reason it has to be
# scanned. Workflows are in because GitHub's default `bash -e {0}` has no
# pipefail, so a `run:` block is exposed exactly when it opts in.
declare -A in_scan=()
declare -a scan=()
for f in "${candidates[@]}"; do
	# grep reads the file directly: no pipe, so no writer to kill. This guard
	# must not contain the shape it bans.
	grep -qE "$pipefail_set" "$f" || continue
	in_scan[$f]=1
	scan+=("$f")
done
i=0
while [ "$i" -lt "${#scan[@]}" ]; do
	f="${scan[$i]}"
	i=$((i + 1))
	while IFS= read -r line; do
		# `. "$script_dir/lib.sh"` -> lib.sh. The directory part is a variable
		# that cannot be expanded statically, so the basename is what gets
		# matched against the candidates. `read` from a here-string, not a
		# pipe: it stops at the first line, which is exactly the early exit
		# this file exists to ban.
		line="${line%%#*}"
		read -r _ path _ <<< "$line"
		path="${path//\"/}"
		path="${path//\'/}"
		lib="${path##*/}"
		for c in "${candidates[@]}"; do
			[ "${c##*/}" = "$lib" ] || continue
			[ -n "${in_scan[$c]:-}" ] && continue
			in_scan[$c]=1
			scan+=("$c")               # grows the worklist: sourcing is transitive
		done
	done < <(grep -E '^[[:space:]]*(\.|source)[[:space:]]' "$f" || true)
done

# --- 1. Every file that turns pipefail on is actually scanned ---------------
# The all-clear below is only worth the paper it is printed on if the scan
# covers what it claims to. Asserting "at least one file was scanned" would not:
# this file always matches itself, so it would catch nothing short of total
# breakage. What is asserted instead is the property that shrinks when coverage
# does -- a pipefail script that the globs above do not reach.
{
	stray=""
	while IFS= read -r f; do
		[ -n "${in_scan[$f]:-}" ] || stray="$stray ${f#"$ROOT"/}"
	done < <(grep -rlE "$pipefail_set" "$ROOT" \
		--exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv \
		--exclude-dir=.claude --exclude-dir=dist --exclude-dir=.bin || true)
	if [ -z "$stray" ]; then
		ok "every file that runs 'set -o pipefail' is in the scan (${#scan[@]} files)"
	else
		fail_test "pipefail file(s) outside the scanned globs -- widen them:$stray"
	fi
}

# --- 2. No branch is decided by a quiet grep reading from a pipe ------------
{
	offenders=""
	for f in "${scan[@]}"; do
		while IFS= read -r line_no; do
			offenders="$offenders ${f#"$ROOT"/}:${line_no}"
		done < <(awk -v qg="$quiet_grep" '
			{
				if ($0 ~ "\\|[[:space:]]*" qg) print NR
				else if (cont && $0 ~ "^[[:space:]]*" qg) print NR
				cont = ($0 ~ /\|[[:space:]]*$/)
			}
		' "$f" || true)
	done
	if [ -z "$offenders" ]; then
		ok "no branch is decided by a quiet grep reading from a pipe"
	else
		fail_test "a quiet grep is fed from a pipe -- SIGPIPE under pipefail (#2447) at:$offenders"
	fi
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
