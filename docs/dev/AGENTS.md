# Agent Operations Manual

Operational knowledge for LLM agents working in this repository — the
agent-facing complement to [`CONTRIBUTING.md`](CONTRIBUTING.md). Every rule
there (issues, branches, commits, PRs, review, merge) applies unchanged; this
document adds what agents need beyond it: deployment-host ground rules,
worktree mechanics, tooling quirks, and a pre-PR checklist. It is written for
machine consumption and may be reorganized freely for that purpose without
affecting the human-facing docs.

Read before touching anything, in order:
[`../../CLAUDE.md`](../../CLAUDE.md) (behavioral rules — simplicity first,
surgical changes), [`CONTRIBUTING.md`](CONTRIBUTING.md) (the change workflow),
[`TESTING.md`](TESTING.md) (TDD discipline).

## 1. The primary checkout is the live-deploy build source

On the canonical host, the repo root checkout is what `docker compose` builds
and deploys (`compose.yaml` + `.env`; [`DEPLOYMENT.md`](DEPLOYMENT.md)
Section 4). A stray branch or dirty tree silently changes what the next
rebuild ships (issue #432).

- **Never** run `git checkout` / `git switch` / `gh pr checkout` in the repo
  root. All branch work happens in a worktree (Section 2).
- The `post-checkout` hook auto-restores the root checkout to `main` whenever
  a checkout moves it off (issue #809). Edge cases:
  - **Dirty tree** — auto-restore is refused to protect the changes; a loud
    error is printed. Stash or commit, then `git checkout main` manually.
  - **Intentional inspection** — set `MCSD_ALLOW_PRIMARY_BRANCH=1` to suppress
    auto-restore for a checkout. The variable persists for the shell's
    lifetime — `unset MCSD_ALLOW_PRIMARY_BRANCH` when done, or subsequent
    checkouts in the same shell are also permitted.
  - **In-progress `git rebase` / `bisect` / `cherry-pick` / `merge`** — the
    hook exits silently (these invoke `post-checkout` internally; restoring
    mid-operation would corrupt them).
- Do not run `docker compose`, stop containers, or rebuild images as a side
  effect of development work — that operates the live stack. Deployment is
  its own task ([`DEPLOYMENT.md`](DEPLOYMENT.md)).
- The live API occupies **port 8000** on the host. Anything started for
  testing runs on alternate ports (webui dev server:
  `VITE_API_PROXY_TARGET=http://localhost:<port> npm run dev`).

## 2. Worktree lifecycle

1. **Create** — `git worktree add <path> -b <branch>` (agent worktrees live
   under `.claude/worktrees/`), or `git checkout -B <branch>` inside an
   already-provisioned worktree. Branch naming per CONTRIBUTING.md Section 3.
2. **Bootstrap once** — `make bootstrap`. A fresh worktree has no
   `webui/node_modules` or `api/.venv`; without it the pre-push `make check`
   dies early (`biome: not found` from webui-lint, and the first api `uv run`
   pays a cold sync).
3. **Hooks are already active** — `core.hooksPath` is repo-local config shared
   by all worktrees; never re-run `make hooks-install` per worktree. The
   `post-checkout` guard stays silent in worktrees (only the root checkout is
   protected).
4. **Clean up after merge** — `git worktree remove --force <path>`. A branch
   held by a lingering worktree blocks `git checkout` / `gh pr checkout` of
   that branch everywhere else, and blocks `gh pr merge --delete-branch`.

## 3. Tooling and account quirks

- Bare `gh pr view <N>` **errors** on this account's token (it queries the
  retired Projects-classic API). Always pass `--json ...`, or use the REST
  form `gh api repos/{owner}/{repo}/pulls/<N>`.
- `gh pr update-branch` does not exist in the installed `gh`. Use:
  `gh api -X PUT repos/{owner}/{repo}/pulls/<N>/update-branch`.
- All agents share one GitHub account, so a PR's author identity can never
  formally approve it (`--approve` fails). Reviews land as comments
  (`gh pr review <N> --comment`); state the verdict in the body, first line
  exactly `VERDICT: APPROVE` or `VERDICT: REQUEST-CHANGES`.
- Checking out a PR branch that another worktree holds fails. Fallback:
  `git fetch origin pull/<N>/head && git checkout --detach FETCH_HEAD`.
- `main` branch protection: required status `check` + strict up-to-date. The
  merge sequence is update-branch → wait for checks (`gh pr checks <N>
  --watch`) → squash-merge (CONTRIBUTING.md Section 7).

## 4. Pre-PR checklist (monorepo tripwires)

- `make check` green locally — the same gate as pre-push and CI.
- `proto/` changed → one atomic change set: `make proto-gen`, update `api/`
  **and** `worker/` together; an intentional contract break carries the
  `breaking` label (CONTRIBUTING.md Section 5).
- api routes/schemas changed → `make openapi-gen`; `make check` has a drift
  gate.
- Generated stubs (`api/src/mcsd/`, `worker/internal/controlplane/`) are
  never hand-edited — regenerate instead.
- A new Alembic migration chains off `main`'s current head at the final
  rebase before merge; expect a renumber whenever another open PR also
  touches `api/migrations/` (CONTRIBUTING.md Section 5).
- Exactly one category label; `Resolves #N` on its own line; short imperative
  title; everything in English.
