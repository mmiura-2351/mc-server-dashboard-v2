# Backlog run

**You own the exit condition. You coordinate. Other agents write and review.**

Use this for "process the backlog", "run the dev loop", and work that goes on
while the owner is away. Do not edit repository files yourself during the run.

1. **List the queue:** the open issues labelled `approved`.

   ```sh
   gh issue list --state open --label approved --json number,title
   ```

   If the list is empty, report "nothing is approved" and stop. Never pick an
   issue that lacks the label, and never add the label during a run unless
   the owner names the issue. Run the command again each time you finish an
   issue: an issue the owner approved meanwhile joins the queue, and an issue
   that lost the label leaves it.
2. **Read each queued issue with its comments**
   (`gh issue view <N> --json title,body,comments`). A comment can defer the
   issue or change its target. Follow it, or say on the issue why you do not.
3. **State the exit condition:** a fresh list shows no issue that you have
   not merged or set aside with a reason. Stop when it holds. Do not wait for
   the owner to approve more.
4. **Start the log.** Keep one file in your scratch directory with one row per
   decision: the time, the issue, what you decided, why, a pointer to the
   evidence (a pull request, a commit, a command), and the result. Add rows;
   never rewrite one. After a context reset, rebuild your picture from the log
   and from `gh pr list --state open`.
5. **Order the queue.** Dependencies come first. Run at most three writing
   agents at once (`docs/dev/AGENTS.md` Section 2). Work that touches
   `api/migrations/`, `proto/`, or the same files runs one after another.
6. **For each issue:** check that it has "Done when" checks (the Start steps
   in `SKILL.md`), give it to a writing agent with the matching playbook
   (`delegation.md`), then follow `land-pr.md`.
7. **What comes up during the run** goes where `AGENTS.md` Section 5.3 sends
   it. File defects. Collect proposals for the report. When something outside
   the queue blocks a queued issue, such as a flaky gate or a broken tool,
   set that issue aside and go on with the rest. A flaky gate is a defect. A
   problem with this machine is not: list it under Attention.
8. **A failed or stalled agent.** Judge it by what it did (commits, pushes,
   files), not by its status text. Retry once with a brief that names the
   failure. After a second failure, set the issue aside with the reason.

**Reply:**

- **Queue.** Each issue with its merged pull request, or with the reason it
  was set aside.
- **Counts.** Issues opened in this run, by label, and issues closed.
- **Defects filed.** The issue numbers.
- **Proposals.** One line each. For a mistake that happened twice, name what
  would prevent it, trying in this order: a simpler structure, a type, a lint
  or check, a test, a line of documentation.
- **Attention.** What the owner should look at first.
- **Log.** The path of the log file.
