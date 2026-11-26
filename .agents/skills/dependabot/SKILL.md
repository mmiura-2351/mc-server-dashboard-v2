---
name: dependabot
description: Triage and merge the open dependency-update pull requests - Dependabot's, and the weekly Python lock refresh that the mc-server-dashboard-v2-bot App opens. Use only when the owner asks to process, triage, or merge dependency updates. A backlog run never starts it.
---

# Dependency-update pull requests

**You own the decision on each pull request: merge it, fix it, or leave it
open.**

Root `AGENTS.md` holds the rules and `docs/dev/DEPENDENCIES.md` holds the
dependency policy. Where this skill disagrees with either, they win.

Run this only when the owner asks. Their request is what allows the merges
(`AGENTS.md` Section 5.1). Copy the steps into your todo list before you start.

## Steps

1. **List the queue.** That list is the whole run.

   ```sh
   gh pr list --state open --author "app/dependabot" --json number,title,headRefName
   gh pr list --state open --author "app/mc-server-dashboard-v2-bot" --json number,title,headRefName
   ```

   The second command finds the lock refresh. It changes only `api/uv.lock`
   and carries no Dependabot metadata.
2. **Order the queue** (see Order).
3. **Take one pull request at a time:**
   1. Bring it up to date with `main`:
      `gh api -X PUT repos/{owner}/{repo}/pulls/<N>/update-branch`.
      Every merge puts the other pull requests behind `main`, and a check that
      ran on an old base proves nothing.
   2. Wait for the required checks: `gh pr checks <N> --required --watch`.
      Then read every check: `gh pr checks <N>`. To read a failed job's log,
      take the job id from the end of the check's URL:
      `gh api repos/{owner}/{repo}/actions/jobs/<job-id>/logs`.
      `gh run view --log-failed` prints nothing with the `gh` on this host.
   3. Read the diff: `gh pr diff <N>`.
   4. Decide (see Decisions).
   5. To merge: `gh pr merge <N> --squash --delete-branch`. Then read the
      state back: `gh pr view <N> --json state --jq .state` must print
      `MERGED`.
4. **Re-sync the root checkout** after the run (see the end of
   `references/failure-patterns.md`).

## Decisions

| What you see | What you do |
|---|---|
| The required checks are green, and every other red check is proven unrelated (below) | Merge |
| A failure matches a pattern in `references/failure-patterns.md` | Apply that pattern's fix, then decide again |
| A major version bump | Read the upstream migration notes (`docs/dev/DEPENDENCIES.md` Section 1). Merge only when the checks are green and the notes show nothing this repository uses is broken. Otherwise leave it open |
| `supply-chain-cooldown` is red | Leave it open (see Cooldown) |
| The pull request causes the failure, or you cannot tell | Leave it open |

**To leave a pull request open,** comment on it with the failing check and
its failure signature, and list it under Attention in your report. If a
comment there already gives the same signature, do not add another. Do not
file an issue for it: the open pull request is the record.

### A red check that is not required

A red check blocks nothing by itself, and it is not safe to ignore by itself.
After you update the branch:

1. Read the failed job's log and find its failure signature: the package,
   version, or advisory id, or the exact failing test.
2. Show that the same signature is on current `main`.
3. Read the diff. If it touches the same ecosystem or lockfile, show that it
   does not change the package or version the failure names.

When all three hold, the failure is unrelated: write the evidence in a comment
on the pull request and merge once the required checks pass. A matching job
name alone is not evidence. A failure that is on `main` is a defect on `main`:
record it as `AGENTS.md` Section 5.3 says.

## Cooldown

`docs/dev/DEPENDENCIES.md` Section 3 forbids adopting a release younger than
seven days.

- **A Dependabot pull request:** the required `supply-chain-cooldown` status
  decides, and it already applies the exemption for security updates. Do not
  check dates by hand, and do not work around a red status. A release that is
  too young turns green by itself, because the check re-runs every day.
- **The lock refresh:** its status is green for every pull request that is not
  Dependabot's, so it proves nothing. The cooldown was applied when the bot
  resolved the lock. Check that nothing else changed it:

  ```sh
  gh pr view <N> --json commits --jq '.commits[] | "\(.oid[0:9]) \(.authors[0].login) \(.messageHeadline)"'
  ```

  A normal list has one commit by `github-actions[bot]`, named
  `chore(deps): refresh Python lockfile`. After it there may be merges of
  `main` from `update-branch`, and companion commits. Each of those carries
  the operator's login. Any other commit needs an explanation before you
  merge.

- **A pin you change by hand** in a companion commit: check its date yourself,
  as `docs/dev/DEPENDENCIES.md` Section 6 says.

## Order

1. `github-actions`. They change CI itself, so later checks run on the new
   versions.
2. `docker` base images (`/api`, `/worker`, `/relay`).
3. `docker-compose` service images.
4. `gomod` for `/worker` and `/relay`. When both update the same dependency,
   such as `google.golang.org/grpc`, merge them one right after the other.
   Never leave one merged and the other open.
5. `pip`. Wait for the `regenerate` check to finish first: it rewrites
   `api/uv.lock` on the branch.
6. The lock refresh, after every `pip` pull request, because those also change
   `api/uv.lock`. If `update-branch` reports a conflict, do not resolve
   `uv.lock` by hand. Run `gh workflow run python-lock-refresh.yml`: it
   rebuilds the lock on current `main` and updates the pull request, or closes
   it when `main` already has everything.
7. `npm` (`/webui`, `/webui/tools/openapi`).

## Companion commits

Some bumps need a second change that Dependabot cannot make.
`references/failure-patterns.md` says which. That commit is your code:

1. Work in a worktree on the pull request's branch
   (`docs/dev/AGENTS.md` Section 2). Add commits only. Never rebase or
   force-push the branch.
2. Push through the hooks. Never use `--no-verify`
   (`docs/dev/CONTRIBUTING.md` Section 4).
3. Before the merge, an agent that did not write the commit reviews it with
   `.agents/skills/review-change/SKILL.md` (`AGENTS.md` Section 5.1). A
   dependency pull request has no "Done when" list, so give the reviewer one:
   the pattern's check command passes, and the commit changes nothing else.
4. Remove the worktree and its local branch before you merge. Otherwise
   `--delete-branch` fails on the local branch after the merge has succeeded.

## Reply

- **Merged.** Each pull request and what it bumped.
- **Left open.** Each pull request and why.
- **Companion commits.** The pull request, what the commit changed, and who
  reviewed it.
- **Defects filed.** The issue numbers.
- **Proposals** and **Attention**, as in every report
  (`.agents/skills/dev-mode/SKILL.md`).
