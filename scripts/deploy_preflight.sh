#!/usr/bin/env bash
#
# deploy_preflight.sh: guard the live deployment against building the wrong ref.
#
# The primary checkout is the deploy build source -- `docker compose` builds it
# from compose.yaml + .env at the repo root (docs/dev/DEPLOYMENT.md Sections 4
# and 8). Agent sessions have repeatedly left it off `main` (#432), so a rebuild
# would silently ship a stray branch or detached HEAD. Run this before any
# `docker compose up -d --build`; it refuses (exit 1) when the checkout is not on
# `main` or the working tree is dirty.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
	echo "deploy preflight: not a git checkout (run from the repo root)." >&2
	exit 1
}
cd "$repo_root"

fail=0

branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")"
if [ "$branch" != "main" ]; then
	ref="${branch:-detached HEAD ($(git rev-parse --short HEAD 2>/dev/null || echo unknown))}"
	echo "deploy preflight: checkout is on '${ref}', not 'main'." >&2
	echo "  The deploy source must be 'main'. Restore it: git checkout main" >&2
	fail=1
fi

if [ -n "$(git status --porcelain)" ]; then
	echo "deploy preflight: working tree is dirty." >&2
	echo "  Commit, stash, or discard local changes before deploying:" >&2
	git status --short >&2
	fail=1
fi

if [ "$fail" -ne 0 ]; then
	echo "deploy preflight: refusing to deploy." >&2
	exit 1
fi

echo "deploy preflight: on clean 'main' -- ok to build."
