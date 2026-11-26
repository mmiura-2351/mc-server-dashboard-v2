# Contributing Workflow

How a change moves from issue discovery and implementation planning through
branches, commits, pull requests, review, merge, and issue closure. Behavioral
guidance for *how to write the code*, and the project rules this workflow
implements, are in [`../../AGENTS.md`](../../AGENTS.md); documentation
conventions are in [`../README.md`](../README.md).

> The commands below are enforced by the git hooks (Section 4; the pre-push
> `make check`) and by the CI workflows (`.github/workflows/`).

> **LLM agents** — and anyone working on the shared deployment host — must
> also read [`AGENTS.md`](AGENTS.md) in this directory, the agent-facing
> operational manual (host ground rules, worktree mechanics, tooling quirks).
> Everything in this document applies there unchanged.

## 1. Issues

The tracker is a record. An issue becomes work only when the repository owner
labels it `approved`. Nobody else applies that label.

- **A defect you find while doing other work** is not fixed in the current PR.
  The lead of the session files it, one issue per defect:
  1. Search the open issues for the same cause first
     (`gh issue list --state open --search "<symbol or error text>"`). If one
     exists, add your evidence there as a comment and file nothing.
  2. Give the `file:line`, the problem in one or two sentences, the command or
     input that shows it, and what a user or operator sees.
  3. Label it `bug`, the module label, and `low-priority`. Leave `low-priority`
     off only when a user or operator would hit the defect in normal use at
     the scale of [`../REQUIREMENTS.md`](../REQUIREMENTS.md) Section 1.1.

  A subagent does not file issues. It reports the defect to the lead.
- **A problem with the local machine or the host** is never an issue: an old
  or missing local tool, a full disk, agents that got in each other's way. A
  pull request cannot fix the machine, so an issue for it can never close. Ask
  one question: does following the docs, and nothing else, reproduce it?
  - Yes: the docs are wrong or incomplete. That is a defect in the docs, and
    it is filed like any other defect. Search the docs for the command or the
    requirement first, to be sure they say it.
  - No: report it to the owner in your final report, and file nothing.
- **An idea for new work** — a feature, a refactor, tooling, a process change,
  a decision the owner must make — is not filed by an agent. It goes in the
  final report as a proposal, and the owner files what they want done.
- A `low-priority` issue stays open until the owner approves or closes it.
  Nothing closes it automatically.

| Issue label | Meaning | Who applies it |
|---|---|---|
| `approved` | This is work to do | The owner only |
| `low-priority` | A recorded defect that nobody works on until it is approved | Whoever files it |

## 2. Plan before implementing

1. Create a branch (Section 3) and attach it to the issue.
2. Sketch the change (a task list or a short plan) before editing code.
3. If the issue is large, split it into sub-issues first; one branch per
   sub-issue.
4. The PR opened from the branch closes the issue with `Resolves #N`.

## 3. Branches

- `fix/issue-{N}-{slug}` for bugs, `chore/issue-{N}-{slug}` for
  release/CI/build maintenance with no user-facing effect (the `chore` label
  of Section 5), and `feature/issue-{N}-{slug}` for everything else. One issue
  per branch.
- If no issue exists, omit the `issue-{N}-` segment: `fix/{slug}`,
  `chore/{slug}` or `feature/{slug}`.

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

- Don't bypass failing pre-commit / pre-push hooks; fix the cause. A failed
  **pre-commit** means the commit did not happen; a failed **pre-push** means
  the push did not happen but the commit stands. Either way, fix the cause in a
  **new** commit rather than `--amend` — the branch is squashed at merge
  (Section 7), so the extra commit costs nothing.
- Commit messages are English, with a short imperative subject.

## 5. Pull requests

- **Title**: short imperative ("Fix Y", not "Fixed Y" or "Y fix").
- **Body**: include `Resolves #N` (or `Fixes` / `Refs`) on its own line when a
  related issue exists; omit it when there is none. Then, in at most 40 lines:
  - `## Why` — the goal, in a sentence or two.
  - `## What changed`.
  - `## Done when` — the checks that define done, each one able to pass or
    fail. Take them from the issue. If the issue has none, write them before
    you write code.
  - `## Verification` — for each check, the command you ran and its result.

  Longer evidence goes in a PR comment.
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
  than one open PR touches `api/migrations/`. The migration guard fails on
  duplicate heads or numbers locally (`make check`) and in CI; CI checks the
  merge ref, so it sees the collision the moment `main` moves, but only
  re-runs on the next push. The rebase-time renumber is the discipline that
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

Review against the PR's `Done when` list, and try to break the change. A
finding needs evidence: the command or test you ran and its output, or the
exact input and the wrong result. Each finding links a `file:line` and has one
class:

| Class | What it is | What happens |
|---|---|---|
| `blocking` | A `Done when` check fails. Or the PR introduces a defect that a user or operator would hit in normal use. Or the PR breaks a repository contract (Section 5) or the gate. | Fixed in this PR. |
| `defect` | Any other defect you demonstrated: one that needs unlikely conditions, one with a minor effect, or one that was there before the PR. | Does not block. Filed as an issue (Section 1). |
| `note` | Everything else: style, wording, naming, a test that could pin more, a risk you did not demonstrate, hardening for a failure nobody has observed. | Stays in the review. Never an issue. |

"No findings" is a valid review.

A later round checks that each `blocking` finding is fixed and reviews what the
fix changed. It does not search the rest of the PR again. If the fix adds new
behavior, review that part as new code. A `blocking` defect you notice anywhere
still blocks.

Submit reviews with `gh pr review`. Approve when no `blocking` finding remains.
There is no limit on rounds.

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
