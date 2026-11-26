# AGENTS.md

Instructions for every coding agent in this repository, in any harness.
Sections 1 to 4 are behavioral guidelines. Section 5 holds the project rules.
Section 6 says where everything else lives.

**Tradeoff:** these guidelines bias toward caution over speed. For trivial
tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes,
simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it
work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer
rewrites due to overcomplication, and clarifying questions come before
implementation rather than after mistakes.

---

## 5. Project Rules

These rules apply to every agent and subagent. Where a skill, a playbook, or a
dispatch prompt says otherwise, these rules win.

### 5.1 Where work comes from

- Work on two things only: a task the owner gives you directly in this
  session, and open issues labelled `approved`. Every other issue is a record,
  not a task.
- Only the owner applies the `approved` label. Never add it to anything.
- A run over the backlog fixes its queue when it starts: the `approved` issues
  open at that moment. An issue that appears during the run is not part of it.
- Merge a pull request only for an `approved` issue, or when the owner asks you
  to merge it. Either way, merge only after a review by an agent that did not
  write the code, and with the required checks green.

### 5.2 Scope

- The product serves small communities: tens of servers
  (`docs/REQUIREMENTS.md` Section 1.1). Judge every design and every defect at
  that scale.
- Inside the task, build the design the system should have. The project is
  pre-release, so do not patch around a wrong shape or keep a compatibility
  layer.
- Outside the task, change nothing: no fix for an adjacent problem, no guard
  for a failure nobody has observed, no cleanup in passing.
- "No change is needed" is a valid result. Say why and stop.

### 5.3 What you find along the way

| You found | Do this |
|---|---|
| A defect you can demonstrate, outside your task | Report it to the lead of the session. The lead files one issue per defect, labelled `low-priority` unless a user or operator would hit it in normal use (`docs/dev/CONTRIBUTING.md` Section 1). Nobody works on it until the owner approves it. |
| A problem with this machine and not with the repository: an old or missing local tool, a full disk, agents that got in each other's way | Never an issue. List it under "Attention" in your final report. The one exception: if following the docs alone reproduces it, the docs are wrong, and that is a defect in the docs. |
| An idea for new work: a feature, a refactor, a tool, a process change, a decision the owner must make | List it under "Proposals" in your final report. Do not open an issue. |
| Anything else: a wording or style point, a test that could pin more, a risk you did not demonstrate | Mention it in the review at most. Never an issue. |

### 5.4 Review and tests

- Only three things block a merge: a "Done when" check that fails, a defect
  the change introduces that a user or operator would hit, and a broken
  repository contract or gate. `docs/dev/CONTRIBUTING.md` Section 6 has the
  definitions.
- A change owes one test per "Done when" check, and no more
  (`docs/dev/TESTING.md` Section 6).

### 5.5 This host runs the live deployment

The repository root checkout is the build source of the running service. Work
in a git worktree, never switch branches in the root, and never run
`docker compose` as a side effect. Read `docs/dev/AGENTS.md` before you run
git, docker, or the gate here.

### 5.6 What enforces these rules

| Rule | Enforced by |
|---|---|
| Only the owner applies `approved` | `scripts/agent_guard.py`, run before every shell command by Claude Code (`.claude/settings.json`) and by Codex (`.codex/hooks.json`). Codex runs it only where the project is trusted and the hook has been reviewed with `/hooks`, so not in an untrusted worktree. |
| A subagent does not file issues | The same hook, in Claude Code only: Codex does not tell a hook which agent is calling |
| Branch work stays out of the root checkout | `.githooks/post-checkout` |
| The gate passes before a push | `.githooks/pre-push` |
| Everything else in this section | Review |

A rule with "Review" beside it holds only as long as reviewers apply it. When
the same mistake happens twice, propose a check for it (Section 5.3).

## 6. Where Things Are

| Need | Read |
|---|---|
| What the system must do, and its scale | `docs/REQUIREMENTS.md` |
| Architecture and per-component design | `docs/README.md` (index), `docs/app/` |
| The change workflow: issues, branches, pull requests, review, merge | `docs/dev/CONTRIBUTING.md` |
| Host and tooling hazards for agents | `docs/dev/AGENTS.md` |
| Commands, setup, where code lives | `docs/dev/DEVELOPMENT.md` |
| How to test, and how much | `docs/dev/TESTING.md` |
| Step-by-step playbooks for development work | `.agents/skills/dev-mode/SKILL.md` |
| How to review a change | `.agents/skills/review-change/SKILL.md` |
| Dependency-update pull requests, when the owner asks | `.agents/skills/dependabot/SKILL.md` |

Skills live in `.agents/skills/`. `.claude/skills/` holds symlinks to them,
because Claude Code reads only that directory.
