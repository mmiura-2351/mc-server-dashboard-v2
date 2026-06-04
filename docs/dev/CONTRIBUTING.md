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

## 4. Commits

Install the git hooks once per clone:

```sh
make hooks-install
```

This points `core.hooksPath` at the checked-in `.githooks/`. The **pre-commit**
hook formats and lints the modules with staged changes; the **pre-push** hook
runs the full `make check` (lint + typecheck + test for both ecosystems).

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
