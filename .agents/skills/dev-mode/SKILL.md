---
name: dev-mode
description: The standard way to do development work in this repository. Restates the goal, fixes what "done" means, picks a playbook (investigation, bug fix, feature, refactoring, opening or landing a pull request, or an unattended run over approved issues), and holds the work to evidence. Use for any task that changes code or docs here, and for "process the backlog" or "run the dev loop".
---

# Dev mode

Root `AGENTS.md` holds the rules. This skill holds the steps. Where the two
disagree, `AGENTS.md` wins.

## Start

1. Restate the goal in your own words, and the smallest change that meets it.
2. Write down what "done" means as checks that can pass or fail. Take them
   from the issue or the task. If you cannot derive them, ask the owner. In an
   unattended run, set the item aside and report it.
3. Pick a playbook from the table, read it, and copy its steps into your todo
   list before you do anything else. A step that does not apply stays on the
   list as `n/a: <reason>`.

## Playbooks

| The task | Playbook |
|---|---|
| A question about the code, or a bug whose cause is unknown | `references/investigation.md` |
| Something behaves wrongly | `references/bug-fix.md` |
| New or changed behavior | `references/feature.md` |
| Same behavior, better structure | `references/refactoring.md` |
| A change is ready for review | `references/open-pr.md` |
| An open pull request needs review, fixes, and a merge | `references/land-pr.md` |
| "Process the backlog", "run the dev loop", work while the owner is away | `references/backlog-run.md` |
| "What should I approve?", sorting the open issues | `references/triage.md` |

If no playbook fits, say so, propose the steps, and wait for the owner's go.

## How to work

- **Understand before you change.** Trace the code path the task touches
  before you edit it. Name the data shape before you write logic.
- **A claim counts when a run shows it.** Mark each claim in a report as
  `measured`, `inferred`, or `guess`. Never hand the owner a check you could
  run yourself.
- **Inconclusive is not a pass.** If a check could not run on the real thing,
  say so in those words.
- **Decide what you can undo. Stop for what you cannot.** Make reversible
  choices and say what you chose and why. Stop for a deploy, deleted data, a
  force-push to a shared branch, and anything that touches the live stack
  (`docs/dev/AGENTS.md` Section 1).
- **Say no when no is right.** If the task, or an addition someone suggests,
  does not earn its place, say so.

## Delegating

Read `references/delegation.md` before you start another agent.

## Reports

Each playbook ends with a **Reply** line that lists what the report contains.
Every final report also has these two parts, even when they are empty:

- **Proposals.** Ideas for new work (`AGENTS.md` Section 5.3), one line each.
- **Attention.** What the owner should look at first: a decision that involved
  a real tradeoff, anything verified only in part, anything set aside.
