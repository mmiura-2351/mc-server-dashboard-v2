# Contributing Workflow

How a change moves from an idea to a merged PR: issues, branches, commits, pull
requests, review, and merge. Documentation conventions are in
[`../README.md`](../README.md).

> Hooks and CI are wired (see Section 4 and `.github/workflows/`); the commands
> below are concrete and enforced (the pre-push `make check` and the CI
> workflows).

> **LLM agents** — and anyone working on the shared deployment host — must
> also read [`AGENTS.md`](AGENTS.md), the agent-facing operational manual
> (host ground rules, worktree mechanics, tooling quirks). Everything in this
> document applies there unchanged.

## 1. Issues

- When you spot a bug, missing feature, or improvement that is **out of scope**
  for the change you are making, open an issue rather than expanding the PR.
- Each issue includes: a concrete `file:line` reference, the problem in one or
  two sentences, and a label — `bug` / `enhancement` / `feature-request`.

## 2. Plan before implementing

1. Create a branch (Section 3) and attach it to the issue.
2. Sketch the change (a task list or a short plan) before editing code.
3. If the issue is large, split it into sub-issues first; one branch per
   sub-issue.
4. The PR opened from the branch closes the issue with `Resolves #N`.

## 3. Branches

- `fix/issue-{N}-{slug}` for bugs, `feature/issue-{N}-{slug}` for everything
  else. One issue per branch.
- If no issue exists, omit the `issue-{N}-` segment: `fix/{slug}` or
  `feature/{slug}`.

Development is **worktree-based**: the root checkout stays on `main`, and each
branch lives in its own worktree
(`git worktree add <path> -b feature/issue-{N}-{slug}`). The checked-in
`post-checkout` hook enforces this by restoring the root checkout to `main`
whenever a checkout moves it off (Section 4); to intentionally inspect a
branch in the root checkout, see the override in [`AGENTS.md`](AGENTS.md).

A freshly created worktree has no installed dependencies; run `make bootstrap`
in it once (Section 4) before pushing, otherwise the pre-push `make check`
cannot run.

## 4. Commits

Install the git hooks once per clone:

```sh
make hooks-install
```

This points `core.hooksPath` at the checked-in `.githooks/`:

- **pre-commit** formats and lints the modules with staged changes.
- **pre-push** runs the full `make check` (lint + typecheck + test for all
  modules) — the same gate CI enforces.
- **post-checkout** keeps the root checkout on `main` (the worktree model,
  Section 3). It stays silent in worktrees; overrides and edge cases are in
  [`AGENTS.md`](AGENTS.md).

A fresh worktree also needs its dependencies installed once — each worktree
keeps its own `webui/node_modules` and `api/.venv`, so a newly created one
starts empty:

```sh
make bootstrap
```

Without it the **pre-push** `make check` fails before it can run. The target
runs `npm ci` (webui) and `uv sync` (api).

- Don't bypass failing pre-commit / pre-push hooks; fix the cause. If a hook
  fails, the commit did not happen — make a **new** commit rather than
  `--amend`.
- Commit messages are English, with a short imperative subject.

## 5. Pull requests

- **Title**: short imperative ("Fix Y", not "Fixed Y" or "Y fix").
- **Body**: include `Resolves #N` (or `Fixes` / `Refs`) on its own line when a
  related issue exists; omit it when there is none.
- Each PR carries **exactly one** category label — these drive the generated
  release notes (see [`RELEASING.md`](RELEASING.md)). Pick the single best fit:

  | Label | When to use | Release-notes group |
  |---|---|---|
  | `breaking` | Backwards-incompatible change (HTTP API, `proto/` contract, or operator-visible behavior) | Breaking Changes |
  | `feature-request` | New capability beyond the current scope | Features |
  | `enhancement` | Improvement to existing functionality | Features |
  | `bug` | Bug fix | Bug Fixes |
  | `documentation` | Docs-only change | Documentation |
  | `dependencies` | Dependency updates | *excluded* |
  | `chore` | Release/CI/build maintenance with no user-facing effect | *excluded* |
- In this monorepo, a PR that changes the `proto/` contract updates `api/` and
  `worker/` together; never merge a contract change that leaves one side
  uncompiled or unimplemented.
- An intentional contract break carries the `breaking` label, which both drives
  the release-notes group and skips the buf-breaking CI gate (see
  [`../../proto/README.md`](../../proto/README.md)); the version bump follows
  [`RELEASING.md`](RELEASING.md) Section 1.
- A PR that adds an Alembic migration renumbers it to `main`'s current head at
  the final rebase before merge: parallel PRs each chain off the same head, so
  whichever merges second collides until renumbered. Expect this whenever more
  than one open PR touches `api/migrations/`. CI's migration guard (the api
  workflow) fails on duplicate heads or numbers against the merge ref, but only
  re-runs on the next push, so the rebase-time renumber is the discipline that
  prevents the collision.
- PR descriptions and issues are written in English.

## 6. Review

Inspect a PR thoroughly:

- **Description & metadata** —
  `gh pr view <N> --json title,body,state,labels,headRefName`.
- **The changes** — `gh pr diff <N>` (add `--name-only` for just the file list).
- **Inline review comments** —
  `gh api repos/{owner}/{repo}/pulls/<N>/comments`. For the PR conversation
  thread, `gh api repos/{owner}/{repo}/issues/<N>/comments`.
- **Run it locally** — `gh pr checkout <N>`.

Submit reviews with `gh pr review`. Group findings by severity: `bug` /
`improvement` / `question` / `nit`. Each finding links a `file:line`. Approve
only after every `bug`-severity item is resolved.

## 7. Merge

Squash-merge by default:

```
gh pr merge <N> --squash --delete-branch
```

The squash commit subject is the PR title; the body is one or two short
paragraphs. Avoid rebase- or merge-commit modes unless explicitly asked.

## 8. Closing issues

An issue is closed only after the PR that addresses it is merged. The closing
comment includes a one-line summary and the PR/commit reference.
