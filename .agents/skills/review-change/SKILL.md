---
name: review-change
description: Review one pull request or diff in this repository. Tries to break the change, keeps only findings that a run proves, classifies them as blocking, defect, or note, and posts a verdict. Use when asked to review a PR, a branch, or a diff, and for re-review after fixes. Run it only in a session that did not write the code.
---

# Review a change

You are the judge, not the author. If you wrote any of this code in this
session, stop and say so.

The definitions live elsewhere, and this skill is the procedure:

- What blocks, the three classes, and later rounds:
  `docs/dev/CONTRIBUTING.md` Section 6.
- How many tests a change owes: `docs/dev/TESTING.md` Section 6.
- Command quirks on this host: `docs/dev/AGENTS.md` Section 4.

Copy the steps of the round you are in into your todo list. A step that does
not apply stays on the list as `n/a: <reason>`.

## First round

1. **Read the change.** Run
   `gh pr view <N> --json title,body,labels,headRefName,headRefOid`, then
   `gh pr diff <N> --name-only`, then the full diff. Read the linked issue with
   its comments.
2. **Write down the intent:** the "Done when" list from the pull request. If
   it has none, derive the list from the issue and say in the review that you
   did.
3. **Check out the head** in your own worktree and run `make bootstrap` once.
   Never touch the repository root.
4. **Run each "Done when" check on the real thing:** the test, the command,
   the request. For each check, confirm that its test fails when you undo the
   production change. Your tree is clean, so `git checkout -- <path>`
   restores the file afterwards (`docs/dev/AGENTS.md` Section 3). Do this for
   the "Done when" checks only. Do not hunt for other mutations that survive.
5. **Try to break the change** with inputs and states that a user or operator
   would produce. Keep a finding only when you ran something that shows it:
   the command, its output, and how the output breaks the intent. A concern
   you did not run is a `note` at most.
6. **Check the scope.** Does the diff change anything the task did not ask
   for? Does it keep the repository contracts
   (`docs/dev/CONTRIBUTING.md` Section 5)?
7. **Classify each finding** and post the verdict.

## Later rounds

1. Fetch the new head.
2. For each `blocking` finding from the last round, run its command again.
3. Review what the fix changed. If the fix adds new behavior, review that part
   as new code.
4. Do not search the rest of the pull request again, and do not repeat a
   finding you already posted. A `blocking` defect you notice anywhere still
   blocks.
5. Post a new verdict with the round number.

## The verdict

All agents share one GitHub account, so a formal approval is impossible. Post
a comment instead (`gh pr review <N> --comment --body-file <file>`, with a
file name no other agent would pick):

```text
VERDICT: APPROVE            (or: VERDICT: REQUEST-CHANGES)
Round <n>, head <sha>

Blocking
- <file:line> <what is wrong>. Ran: <command>. Got: <output>. Intent: <which check it breaks>.

Defects (not blocking)
- <file:line> <what is wrong>. Ran: <command>. Got: <output>.

Notes
- <at most five, one line each>

Dropped: <how many concerns you looked at and did not keep, and why in one line>
NEEDS-RUNTIME-VERIFY: <what to exercise>   (only when code and tests cannot show it)
```

Write `none` under a heading that has no entries. Approve when "Blocking" is
`none`. "No findings" is a valid review.

## Report to the lead

Send back the verdict, the round, the findings by class, the commands you ran
with their results, and the `NEEDS-RUNTIME-VERIFY` line if there is one.
