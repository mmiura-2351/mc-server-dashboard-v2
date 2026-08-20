# Agent Operations Manual

Operational knowledge for LLM agents working in this repository — the
agent-facing complement to [`CONTRIBUTING.md`](CONTRIBUTING.md). Every rule
there (issues, branches, commits, PRs, review, merge) applies unchanged; this
document adds what agents need beyond it: deployment-host ground rules,
worktree mechanics, silently failing commands, tooling quirks, and a pre-PR
checklist. It is written for machine consumption and may be reorganized freely
for that purpose without affecting the human-facing docs.

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
    auto-restore for a checkout; a loud notice is still printed. The variable
    persists for the shell's lifetime — `unset MCSD_ALLOW_PRIMARY_BRANCH` when
    done, or subsequent checkouts in the same shell are also permitted.
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

## 3. Commands that fail silently

Each one succeeds, or appears to; the damage surfaces later.

- **The scratchpad is shared across concurrently running agents, not
  session-private, so a backup named after its source collides by
  construction.** Mutation-testing production code to prove a test is a real
  pin has an edit-then-revert shape; the reflex revert `git checkout <path>`
  restores from the index, not `HEAD`, eating any unstaged edits with no
  confirmation, no reflog entry, nothing to recover from — so PR #2521 copied
  the file aside and restored from the copy instead. But two agents mutating
  the same file — normal, not exotic, when several review or implement against
  one module — both write `<scratchpad>/app.py.orig`; the restore then silently
  writes the *other* agent's file into this worktree, with no error, no conflict
  marker, and a plausible-looking `git diff` (PR #2591). Choose the revert by
  the file's state: `git checkout -- <path>` when it has no unstaged edits
  (collision-proof, and a mutation on an otherwise-clean file has none); a
  uniquely-named copy — `mktemp`, or embed the agent id — when it does.
- **`--no-verify` cannot establish what the gate establishes.** The rule is in
  [`CONTRIBUTING.md`](CONTRIBUTING.md) Section 4; it is unconditional because a
  bypass is only known to have been harmless *afterwards* — the very fact the
  gate exists to establish beforehand. "Only the known flake" is a prediction,
  not a result. Escalate a flaky gate as an issue (#2513) and re-run instead
  (PR #2517).
- **`pgrep -f <pattern>` matches the waiting shell itself.** `pgrep` omits only
  its own process, not the shell that invoked it — whose command line contains
  the pattern. So `until ! pgrep -f "make check"; do sleep 30; done` never
  exits, and a different token does not help (`pgrep -f check_parallel.sh`
  self-matches identically). Run the command in the foreground and let it
  block; if a poll is genuinely needed, bracket one character so the pattern
  cannot match its own literal text — `pgrep -f "make chec[k]"` matches the
  gate but not the waiter. A self-deadlocked waiter is indistinguishable from a
  contended host (#2513), so it never diagnoses itself.
- **Killing a backgrounded `git push` does not kill the gate it started.** The
  push dies; the pre-push tree it spawned — `make check` →
  `scripts/check_parallel.sh` → sub-makes → pytest — keeps running in that
  worktree. While the orphan lives, the next gate run there has been seen dying
  mid-suite (`api-test Terminated`, at 42%): a red that looks exactly like the
  #2228 / #2513 timeout flakes, in the same fs-heavy modules, and that a re-run
  does not clear — so the reflex response buys another full gate of the same
  (issue #2605, found on PR #2603). Before attributing a red to a flake in a
  worktree whose push was interrupted, look for the survivor.
  `pgrep -af "<worktree-pat[h]>"` names `scripts/check_parallel.sh` and its
  chain subshells (forks share its argv): `make check` passes `$(CURDIR)` to it
  for exactly this purpose. Bracket one character of the path every time, even
  for a one-shot — the shell that runs the `pgrep` carries the pattern in its
  own command line (previous entry), and here a self-match costs more than a
  stuck poll: it reads as a survivor, and killing its process group kills the
  session doing the diagnosing. Nothing below those carries the path
  (sub-makes, pytest and vitest all run with a bare argv), so a stray child is
  identified only by `readlink /proc/<pid>/cwd`. Kill by process group rather
  than by pid — `pgid=$(ps -o pgid= -p <pid> | tr -d ' '); kill -- -"$pgid"` —
  because the script's own TERM trap reaches its subshells but not their
  sub-makes. Preventing the orphan (one gate at a time per host, via `flock`)
  is decided on #2513, not here.
- **`uv run --active` in a worktree re-points the primary checkout's
  `api/.venv`.** Worktree shells inherit `VIRTUAL_ENV` from the repo root;
  plain `uv run` ignores it and uses the worktree's own `.venv`, but `--active`
  adopts the inherited one and installs the *branch's* `api/src` into it, so
  the primary checkout imports branch sources. Nothing reports this: the
  `api-env-check` preflight (`scripts/check_api_env.py`, issue #566) would fail
  on the mismatch, but the plain `uv run` in front of it re-syncs the damage
  away first, so the gate prints `OK`. Never pass `--active`; repair a checkout
  explicitly with `cd api && uv sync`.

## 4. Tooling and account quirks

- Bare `gh pr view <N>` **errors** on this account's token (it queries the
  retired Projects-classic API). Always pass `--json ...`, or use the REST
  form `gh api repos/{owner}/{repo}/pulls/<N>`.
- `gh pr edit <N>` **fails the same way** (Projects-classic GraphQL):
  `--add-label` / `--remove-label` leave labels unchanged, and `--body`
  silently no-ops. Avoid `gh pr edit` entirely; use the REST equivalents —
  `gh api -X PATCH repos/{owner}/{repo}/pulls/<N> -f body=...` for title/body,
  and `gh api -X POST repos/{owner}/{repo}/issues/<N>/labels
  -f "labels[]=<label>"` / `gh api -X DELETE
  repos/{owner}/{repo}/issues/<N>/labels/<label>` for labels.
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

## 5. Pre-PR checklist (monorepo tripwires)

- **Don't run the full `make check` by hand before pushing.** The pre-push
  hook runs exactly it, so a manual run pays the whole gate twice on an
  unchanged tree — 10-40 min each on a contended host, and two chances at the
  #2228 / #2513 timeout flakes instead of one (issue #2574). Iterate with the
  targeted subset instead: `make <module>-lint` / `make <module>-test` for the
  touched module (`api`, `worker`, `relay`, `webui`), `make proto-lint` for
  `proto/`, `make docs-check` for docs, `make migrations-check` for
  `api/migrations/` (~0.1 s, and a late failure there costs a rebase renumber).
  Let the hook be the single full-gate run: a failed **pre-push** hook leaves
  the commit intact and only the push undone, so the fix is a follow-up commit,
  squashed away at merge (CONTRIBUTING.md Section 4). The gate itself is
  unchanged — never `--no-verify` (Section 3).
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
- Exactly one category label; `Resolves #N` on its own line when a related
  issue exists (omit it when there is none); short imperative title; everything
  in English.
