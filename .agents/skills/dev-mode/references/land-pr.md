# Landing a pull request

**You own the merge. The agent that wrote the code never judges it.**

Land a pull request only for approved work (`AGENTS.md` Section 5.1).

1. **Review.** Start a fresh agent that has not seen how the code was written,
   and have it follow `.agents/skills/review-change/SKILL.md` on the pull
   request (`delegation.md`). If you wrote the code in this session, you
   cannot be the reviewer.
2. **Read the verdict yourself.** For each `blocking` finding, send it back to
   the author with the finding quoted in full. If the author's session is
   gone, start a fresh agent with the pull request, the branch, and the
   findings.
3. **Re-review** when the fix is pushed. Use the same reviewer if it is still
   available, and tell it which findings were addressed. Rounds have no limit.
4. **Step in when the loop stops converging:** the same finding comes back
   after a fix that claimed to address it, or the findings drift away from the
   "Done when" list. Read the evidence yourself and rule. Either name one
   final fix, or merge and post your reason on the pull request.
5. **File the `defect` findings** as issues (`docs/dev/CONTRIBUTING.md`
   Section 1). Leave the `note` findings in the review.
6. **Runtime check, only when needed:** the behavior cannot be judged from the
   code and the tests (a UI flow, a process lifecycle, streaming or reconnect,
   a migration against a real database), or the review says
   `NEEDS-RUNTIME-VERIFY`. A fresh agent drives the real flow under the
   runtime-check rules in `delegation.md` and reports `works`, `broken`, or
   `could-not-verify` for each claimed behavior, with evidence. `broken` is a
   `blocking` finding.
7. **Merge one pull request at a time.** Every merge puts the other open pull
   requests behind `main`. Remove the worktree that holds the branch, update
   the branch, wait for the required checks, then squash-merge
   (`docs/dev/AGENTS.md` Section 4). Read the pull request back and confirm
   its state is `MERGED`.
8. **If another open pull request now conflicts,** its author rebases it. A
   migration it adds is renumbered then (`docs/dev/CONTRIBUTING.md`
   Section 5).

A failing check that looks like a flake earns one re-run, with the logs of the
failing run kept first (`docs/dev/AGENTS.md` Section 3).

**Reply:** the pull request, the number of rounds, each `blocking` finding with
the commit that fixed it, the issues you filed, and the merge state you read
back.
