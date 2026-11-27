# Delegating to another agent

A subagent does not see your conversation. It does load root `AGENTS.md`.

## The kickoff

Give every agent you start:

- The goal, in one sentence.
- The issue text and its comments, word for word.
- The file to follow: a playbook under `.agents/skills/dev-mode/references/`,
  or `.agents/skills/review-change/SKILL.md`. Give the path, not the skill
  name.
- The "Done when" checks.
- Constraints the issue does not state.
- What to send back.
- This paragraph, unchanged:

  > The bar in this kickoff is the whole bar. Do not add a check, a gate, or a
  > review round above it. If the base branch already has a problem, report it
  > in one line and move on. Do not file issues, apply labels, or merge:
  > report, and the lead acts.

## Rules

- **Writers get their own worktree** and run `make bootstrap` in it once
  (`docs/dev/AGENTS.md` Section 2). Readers, meaning investigation, review,
  and runtime checks, commit nothing and push nothing. A reviewer works in its
  own worktree too, and restores any file it changes to run a check.
- **Start a fresh agent for each task.** Reuse an agent only for state that is
  costly to move: the author fixing review findings in its own worktree, or
  the reviewer looking at the fix.
- **You own every delegate's work.** Read its diff or its report yourself
  before you act on it. Its summary is a claim, not evidence.
- **If your harness cannot start subagents,** do each step yourself, one after
  another. A review done that way is not independent: say so in the report.

## Runtime checks

An agent that runs the system to check a change follows `docs/dev/AGENTS.md`
Section 1: never `docker compose`, never port 8000, its own worktree only.
Scratch files, screenshots, and logs go in its scratch directory. It stops
everything it started before it reports. When the environment defeats it, it
reports `could-not-verify` with the specifics. A partial check is never
reported as a full one.

## Harness notes

- **Claude Code.** Start a subagent with the `Agent` tool. Pass
  `isolation: "worktree"` for a writer. A completion notice also fires when a
  background agent only yields, for example while it waits on a push. Read the
  result, and act only on a final report: a pull request number or a hard
  failure. Do not push on its behalf.
- **Codex and other harnesses.** Use the harness's own subagent tool. Create
  the writer's worktree yourself:
  `git worktree add .claude/worktrees/<name> -b <branch>`.
