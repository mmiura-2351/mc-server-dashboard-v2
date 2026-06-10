# Contributing Workflow

How a change moves from an idea to a merged PR: issues, branches, commits, pull
requests, review, and merge. Behavioral guidance for *how to write the code* is
in [`../../CLAUDE.md`](../../CLAUDE.md); documentation conventions are in
[`../README.md`](../README.md).

> Hooks and CI are wired (see Section 4 and `.github/workflows/`). Some other
> steps reference mechanisms still being set up; follow the intent, concrete
> commands are added as the toolchain lands.

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

Work in your own worktree, and create the branch there
(`git checkout -B feature/issue-{N}-{slug}`). The primary checkout is the
live-deployment build source (`docker compose` builds it from `compose.yaml` +
`.env` at the repo root), so it must stay on `main` — never `git checkout` /
`git switch` it as part of branch setup; a stray checkout silently changes what
the next rebuild deploys.

## 4. Commits

Install the git hooks once per clone:

```sh
make hooks-install
```

This points `core.hooksPath` at the checked-in `.githooks/`. The **pre-commit**
hook formats and lints the modules with staged changes; the **pre-push** hook
runs the full `make check` (lint + typecheck + test for both ecosystems). The
**post-checkout** hook auto-restores the *primary* checkout (the repo root, not
a worktree under `.claude/worktrees/`) to `main` whenever it is left off it —
it is the deploy build source (Section 3), so a stray `git checkout`, `git
switch`, or `gh pr checkout` is immediately undone. The hook stays silent in
worktrees. Edge cases:

- **Dirty working tree** — if the primary checkout has uncommitted changes when
  the stray checkout occurs, auto-restore is refused to protect those changes; a
  loud error is printed instead. Stash or commit, then restore manually with
  `git checkout main`.
- **Intentional override** — set `MCSD_ALLOW_PRIMARY_BRANCH=1` in the
  environment to suppress auto-restore for a single checkout (e.g. to inspect a
  branch directly). A notice is still printed; restore manually when done. Note:
  this variable persists for the lifetime of the shell process — unset it
  explicitly (`unset MCSD_ALLOW_PRIMARY_BRANCH`) when the inspection is done,
  or subsequent checkouts in the same shell will also be permitted.
- **git bisect / git rebase** — the hook is silently skipped during in-progress
  `git bisect`, `git rebase`, `git cherry-pick`, and `git merge` operations on
  the primary checkout. These operations invoke `post-checkout` internally, and
  restoring to `main` mid-operation would corrupt them. The auto-restore
  adversary (an agent accidentally running `git checkout` or `gh pr checkout`)
  does not create those in-progress state files.

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
  `gh pr view <N> --json title,body,state,labels,headRefName`. The bare
  `gh pr view <N>` errors on this account's token (it queries the retired
  Projects-classic API), so always pass `--json`, or use the REST form
  `gh api repos/{owner}/{repo}/pulls/<N>`.
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
