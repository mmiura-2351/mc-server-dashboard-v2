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
rebuild ships.

- **Never** run `git checkout` / `git switch` / `gh pr checkout` in the repo
  root. All branch work happens in a worktree (Section 2).
- The `post-checkout` hook auto-restores the root checkout to `main` whenever
  a checkout moves it off. Edge cases:
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
- **The dangerous command is the safe-looking one, and the worktree does not
  protect you.** `compose.yaml` pins `name: mcsd`, so an un-overridden
  `docker compose up` in **any** checkout of this repository — the deploy root
  or your own worktree — resolves to project `mcsd`, *which is the live
  deployment*: it recreates the live containers and mounts the live volumes.
  Nothing in the command or the directory signals that. The project name alone
  is not the whole isolation either: a named network is shared rather than
  namespaced by project, so a stack that kept the live network names would
  land its containers on the live `mcsd` network, container-name DNS included,
  even under its own project name — which is why `compose.yaml` derives the
  network names from `${COMPOSE_PROJECT_NAME}` (next bullet).
- **A probe stack is a different *project*, and that means all of these, not
  just the first.** The recipe exists for a sanctioned probe that was
  explicitly asked for; it does not lift the rule above.
  - `COMPOSE_PROJECT_NAME=<unique>` (or `-p <unique>`) — this is what makes it
    a separate stack. Both override the `name:` key, and both flow into the
    `${COMPOSE_PROJECT_NAME}` interpolations in `compose.yaml`, so the networks
    render `<unique>` / `<unique>-servers` and the worker attaches the MC
    containers it creates to `<unique>-servers` (verified on Compose v5.1.4).
  - `--env-file <your own copy>` — never the deploy root's `.env`.
  - a unique `MCSD_SCRATCH_DIR`: it is bind-mounted at the same absolute path
    on the host and in the container, so sharing the live one drops a probe
    worker into the live servers' working directories.
  - its own store and database, never the live ones. The `seaweedfs-data`,
    `api-storage` and `db-data` volumes are scoped to the project, so a
    separate project gets its own. An env-file or override that points
    `MCD_API_STORAGE__OBJECT__ENDPOINT` / `MCD_API_STORAGE__OBJECT__BUCKET` (or
    `MCD_API_STORAGE__FS__ROOT`) at the live store undoes that. A store belongs
    to exactly one database, and the probe's GCs would see every live object as
    an orphan. On a store the live API has claimed, the store owner marker makes
    them refuse to run, with an ERROR
    ([`../app/STORAGE.md`](../app/STORAGE.md) Section 4.3). It does not stop the
    probe's writes, which the live API then reclaims as orphans. Nor does it
    help on an unmarked store (the first start after the upgrade, or a deleted
    marker): a probe that starts first claims it, becomes the owner and GCs the
    live objects (issue #3886). A probe whose database is a dump of the live
    one carries the live installation id and is not refused at all.
  - alternate host ports. `API_HTTP_PORT` (live: 8000) is a variable; the
    `relay` profile's `25565`, `25665`, `25675/udp` and `19132-19231/udp` are
    literals in `compose.yaml` and need an override file. MC server game ports
    are published by the worker at runtime from the API's configured range.
  - Confirm with `docker compose … config` **before** any `up`: the rendered
    `name:`, both `networks.*.name` and the worker's
    `MCD_WORKER_DRIVER_CONTAINER_NETWORK` must all carry `<unique>`.
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

- **Commit before you mutate.** Mutation-testing production code to prove a
  test is a real pin has an edit-then-revert shape, and the reflex revert
  `git checkout <path>` restores from the index, not `HEAD`, eating any
  unstaged edits with no confirmation, no reflog entry, nothing to recover
  from. Reordering the work removes that hazard instead of navigating it: with
  the tree clean before the mutation, both `git checkout -- <path>` and the
  bare form restore the committed content, and no file has to be copied aside,
  so the copy-aside collision below cannot arise either. The branch is squashed
  at merge (CONTRIBUTING.md Section 4), so the extra commit costs nothing — the
  same reason a failed pre-push is fixed in a new commit rather than an amend.
  When a commit genuinely cannot come first, `git checkout -- <path>` on an
  otherwise-clean file is the fallback, still with no copy; but it is the
  ordering, not the spelling of the revert, that protects unstaged work.
- **The scratchpad is shared across concurrently running agents, not
  session-private, so any generically named file a later command reads back
  collides by construction.** Two agents that copy the same file aside —
  normal, not exotic, when several review or implement against one module —
  both write `<scratchpad>/app.py.orig`; the restore then silently writes the
  *other* agent's file into this worktree, with no error, no conflict marker,
  and a plausible-looking `git diff`. Every consumer that reads a file back has
  this shape: `gh pr create --body-file <scratchpad>/pr_body.md` opens a PR
  carrying a concurrent agent's body and its `Resolves #N`, which closes the
  wrong issue at merge. So name uniquely whatever a later command reads back —
  PR and issue bodies, review comments, generated patches, backups: `mktemp`,
  or embed the agent id or branch name.
- **`--no-verify` cannot establish what the gate establishes.** The rule is in
  [`CONTRIBUTING.md`](CONTRIBUTING.md) Section 4; it is unconditional because a
  bypass is only known to have been harmless *afterwards* — the very fact the
  gate exists to establish beforehand. "Only the known flake" is a prediction,
  not a result. Escalate a flaky gate as an issue and re-run instead. The
  re-run keeps the failing run's logs in `check-logs.prev/`, including the api
  suite's per-worker run order ([`DEVELOPMENT.md`](DEVELOPMENT.md) Section 3),
  but only until the next gate run in that worktree replaces them — a second
  re-run, or the next push. Cite or copy them into the issue before running the
  gate again.
  `MCSD_CHECK_LOCK_HELD` is the quiet form of the same move: the gate exports it
  so its own nested runs (`make scripts-test` runs the orchestrator) do not wait
  for the lock they already hold; setting it by hand instead drops the host-wide
  serialisation below — never set it yourself.
- **Re-running the gate past the argon2 flake throws away its only evidence.**
  A correct password has intermittently failed to verify against a hash the
  same process had just made (#3748). The api tests exposed to it (the logins
  in `tests/integration/test_auth_flow.py`, `test_register_user.py` and
  `test_login_dummy_hash.py`, and the argon2 verifies in
  `tests/identity/test_password_hasher.py`) then fail with
  an `AssertionError` whose message is a timestamped diagnostic report, in the
  api chain's log, and the gate's canary (below) writes `argon2-canary.log`
  beside it. Both are in `check-logs/`, or `check-logs.prev/` after one
  re-run, until the next gate run replaces them. Paste the whole report and
  the canary log into #3748 before running the gate again. A login refused by
  a lockout or throttle says so in its first line and skips the probes; that
  is not this flake. To read the report: if the stored hash verifies `True` on
  every re-verify, the refusal was a verify-time transient (for a login, only
  when the chained traceback shows `reason = 'wrong_password'`). If the
  re-verifies fail but the fresh round trips pass, the stored hash is bad;
  `equals the hash the test produced` then says whether it went bad in storage
  or was made bad. If the fresh round trips fail too, the process state is
  persistently bad, and the thread list and the tests that process ran before
  it are where to look. Mixed results, such as only some re-verifies failing,
  or only the lines of one thread, mean a state that comes and goes: say which
  thread labels failed. A KDF-executor probe that times out skips the rest and
  says so. The `host` section is read before the probes, so it shows the host
  as the refusal left it: the pressure stall averages (`some avg10=` is the
  share of the last 10 s in which some task waited for the cpu, memory or io),
  the swap counters, the `getrusage` deltas over the refused call
  (`ru_majflt` above 0 means it waited for a page from disk or swap,
  `ru_nivcsw` counts its preemptions), and the kernel's name for each thread,
  native ones included. The printed hash can be re-verified in a fresh process
  at any time afterwards. `<password>` is the password the failing test used:
  its `_PASSWORD` constant (`_VALID_PASSWORD` in `test_register_user.py`, a
  literal in `test_password_hasher.py`), `Wm7!qz#Lp2vT` in all four, except
  that the login dummy-hash check in `test_password_hasher.py` verifies
  `_DUMMY_VERIFY_PLAINTEXT` from `dependencies.py`. This
  prints `True`, or raises `VerifyMismatchError` when the hash itself is bad:
  ```sh
  cd api && uv run python -c 'import argon2, sys; print(argon2.PasswordHasher().verify(*sys.argv[1:]))' '<hash>' '<password>'
  ```
  The canary is `scripts/argon2_canary.py`, which the gate runs for as long as
  the api chain runs: a separate process, at nice 19, that shares nothing with
  the suite but the host. Every 4–9 s under a gate (a check, then a 2 s
  sleep) it recomputes a fixed Argon2id tag at the production cost with 4
  lanes on 4 threads (`kind=p4`) and with 1 lane (`kind=p1`), each against a
  hard-coded tag, plus a 64 MiB write-and-read-back control without argon2
  (`kind=control`). Every line starts with a UTC timestamp, to set against the
  report's `taken at`: a `MISMATCH` line for any computation that disagrees
  (`check=0` is the one at start-up), `ERROR` for one that raised, a `summary`
  every 30 checks, and a last `stopped` line with its totals (`checks=`, and
  `cpu_s=` over `wall_s=` for its cost). A hit counts whether or not the suite
  flaked in that run, so the gate reads the log as the canary stops: on a
  `MISMATCH` or `ERROR` it prints a `!!! argon2 canary` banner to stderr, even
  in a run that passes, and copies the log to `argon2-canary-hits/` in the git
  common dir (`git rev-parse --git-common-dir`; the primary checkout's
  `.git/`), which neither the log rotation nor a worktree removal reaches.
  Look there, and paste any file in it into #3748. A canary that got no check
  done (it never started, or stopped at `checks=0`) says `argon2 canary did
  not run:` on stderr instead; that run does not count as clean. Read a hit
  this way: an `ERROR` alone is a computation that did not finish, such as a
  failed 64 MiB allocation, not a wrong answer, so it says the host was short
  of something rather than that argon2 computed wrongly. A canary `MISMATCH`
  rules out the suite's process (a fault of the host or of libargon2 itself
  would both show so); in `control` too, it is memory corruption in general,
  and there `first` unlike `second` is a transient read, while both unlike
  `expected` is corrupted memory; `p4` hits with none in `p1` point to
  threads, but only once they repeat, since each kind runs once a check. A
  clean canary is weak evidence: roughly 80–130 argon2 computations a gate
  (40–66 checks so far) is a small sample next to the suite's own argon2
  work, so it takes a clean `argon2-canary-hits/` across many further flakes
  before it points to state inside the suite's own process.
- **`pgrep -f <pattern>` matches the waiting shell itself.** `pgrep` omits only
  its own process, not the shell that invoked it — whose command line contains
  the pattern. So `until ! pgrep -f "make check"; do sleep 30; done` never
  exits, and a different token does not help (`pgrep -f check_parallel.sh`
  self-matches identically). Run the command in the foreground and let it
  block; if a poll is genuinely needed, bracket one character so the pattern
  cannot match its own literal text — `pgrep -f "make chec[k]"` matches the
  gate but not the waiter. A self-deadlocked waiter is indistinguishable from a
  contended host, so it never diagnoses itself.
- **Killing a backgrounded `git push` does not kill the gate it started.** The
  push dies; the pre-push tree it spawned — `make check` →
  `scripts/check_parallel.sh` → sub-makes → pytest — keeps running in that
  worktree. The gate takes a host-global `flock` (`/tmp/mcsd-check.lock`)
  before it does any work and every process it spawns inherits the descriptor,
  so a survivor holds the lock until it finishes and the next run in that
  worktree waits instead of racing it (unserialised, the second run would die
  mid-suite in the same fs-heavy modules with a red indistinguishable from a
  timeout flake, and one that a re-run does not clear). The wait prints
  `held by: <worktree> (pid N, since ...)` — when the worktree it names is your
  own, the holder *is* the survivor. That pid is the orchestrator's, which is
  the right one here but not always: when the orchestrator is itself what died
  (a `kill -9`, or a foreground Ctrl-C whose TERM trap reaches its chain
  subshells but not their sub-makes), those sub-makes keep the lock without it
  and the message names a pid that no longer exists. The gate tests it and
  appends `fuser -v /tmp/mcsd-check.lock` when it is gone. Read that list for
  what it is: every process with the lock file open — the holder, the
  descendants that inherited its descriptor, and any gate queued behind it,
  which has the file open too (one holder and one waiter listed six processes
  here). Nothing in it marks the holder, and `/proc/locks` separates granted
  from blocked but names the `flock` helper rather than the gate, so attribute
  the pids yourself with `readlink /proc/<pid>/cwd`; the `held by:` line
  already names the worktree to match them against. The two diagnostics answer
  different questions — `fuser` says which processes have the lock file open
  at all, the `pgrep` below says which ones belong to a given worktree — and a
  survivor is identified by the pair. Before attributing a stalled or red gate
  in a worktree whose push was interrupted to a flake or a contended host,
  look for the survivor.
  `pgrep -af "<worktree-pat[h]>"` names `scripts/check_parallel.sh` and its
  chain subshells (forks share its argv): `make check` passes `$(CURDIR)` to it
  for exactly this purpose. Bracket one character of the path every time, even
  for a one-shot — the shell that runs the `pgrep` carries the pattern in its
  own command line (the `pgrep` entry), and here a self-match costs more than a
  stuck poll: it reads as a survivor, and killing its process group kills the
  session doing the diagnosing. Nothing below those carries the path
  (sub-makes, pytest and vitest all run with a bare argv), so a stray child is
  identified only by `readlink /proc/<pid>/cwd`. Kill by process group rather
  than by pid — `pgid=$(ps -o pgid= -p <pid> | tr -d ' '); kill -- -"$pgid"` —
  because the script's own TERM trap reaches its subshells but not their
  sub-makes.
- **`uv run --active` in a worktree re-points the primary checkout's
  `api/.venv`.** Worktree shells inherit `VIRTUAL_ENV` from the repo root;
  plain `uv run` ignores it and uses the worktree's own `.venv`, but `--active`
  adopts the inherited one and installs the *branch's* `api/src` into it, so
  the primary checkout imports branch sources. Nothing reports this: the
  `api-env-check` preflight (`scripts/check_api_env.py`) would fail
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
- `gh pr checks <N> --json <fields>` is rejected by the installed `gh`:
  `unknown flag: --json`, then the usage text, exit 1, no JSON. `--watch`
  works. For scriptable check state, `gh pr view <N> --json statusCheckRollup`
  returns both `CheckRun` entries (`name`/`status`/`conclusion`) and
  `StatusContext` entries (`context`/`state`) — a filter has to handle both
  shapes. The REST `repos/{owner}/{repo}/commits/<sha>/check-runs` endpoint
  carries the check runs only, so commit statuses — the required
  `supply-chain-cooldown` among them — are missing from it.
- All agents share one GitHub account, so a PR's author identity can never
  formally approve it (`--approve` fails). Reviews land as comments
  (`gh pr review <N> --comment`); state the verdict in the body, first line
  exactly `VERDICT: APPROVE` or `VERDICT: REQUEST-CHANGES`.
- Checking out a PR branch that another worktree holds fails. Fallback:
  `git fetch origin pull/<N>/head && git checkout --detach FETCH_HEAD`.
- `main` branch protection: required statuses `check`, `supply-chain-cooldown`
  and `live-s3`, + strict up-to-date. The merge sequence is update-branch → wait
  for checks (`gh pr checks <N> --watch`) → squash-merge (CONTRIBUTING.md
  Section 7).

## 5. Pre-PR checklist (monorepo tripwires)

- **Don't run the full `make check` by hand before pushing.** The pre-push
  hook runs exactly it, so a manual run pays the whole gate twice on an
  unchanged tree — 10-40 min each on a contended host, and two chances at a
  timeout flake instead of one. Gates are serialised host-wide (Section 3), so
  a stray manual run also makes every other worktree's push wait for it.
  Iterate with the targeted subset instead: `make <module>-lint` /
  `make <module>-test` for the
  touched module (`api`, `worker`, `relay`, `webui`), `make proto-lint` for
  `proto/`, `make docs-check` for docs, `make migrations-check` for
  `api/migrations/` (~0.1 s, and a late failure there costs a rebase renumber).
  Let the hook be the single full-gate run: a failed **pre-push** hook leaves
  the commit intact and only the push undone, so the remedy is a follow-up
  commit, squashed away at merge (CONTRIBUTING.md Section 4). The gate itself
  is unchanged — never `--no-verify` (Section 3).
- The gate runs the DB-gated api integration tests only when
  `MCD_TEST_DATABASE_URL` is exported (skipped otherwise). Start the scratch
  Postgres with the host-network recipe in
  [`api/tests/integration/README.md`](../../api/tests/integration/README.md):
  a `-p` published port puts all the gate's DB traffic through `docker-proxy`,
  and a container started without the recipe's durability flags (`fsync=off`
  and the two beside it) makes every fixture's migrations wait on the host's
  disk — restart an older one rather than reuse it.
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
- A precondition that test fixtures satisfy **by hand** changed (the launch
  working-set guard's `.mcsd_generation` marker) → `worker-e2e-compile` only
  compiles the `-tags e2e` fixtures, so a stale one fails only in CI. From the
  repo root: `docker build -t mcsd-e2e-stub:latest worker/test/e2e/stub`, then
  `cd worker && MCD_E2E_DOCKER=1 MCD_E2E_STUB_IMAGE=mcsd-e2e-stub:latest
  go test -tags e2e -v -timeout 300s ./test/e2e/...` (keep the `-v`: an
  all-skip run prints `ok` too). `relay/test/e2e/` is tagged the same way; run
  it via `make relay-e2e` / `make bedrock-e2e`.
- Exactly one category label; `Resolves #N` on its own line when a related
  issue exists (omit it when there is none); short imperative title; everything
  in English.
