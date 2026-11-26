# Triage

**You own the recommendation. The owner owns the decision.** This playbook
changes no code and opens no pull request.

Use it when the owner asks what to approve, and to sort a pile of open issues.

1. **List the open issues that are not approved.** If the owner named a part
   of them (a label, a module, a count), take only that part.

   ```sh
   gh issue list --state open --search "-label:approved" --limit 300 --json number,title,labels,createdAt
   ```

2. **Read each issue with its comments**
   (`gh issue view <N> --json title,body,labels,comments`), then the code at
   the `file:line` it names. A long list can be split by module label across
   reading agents (`delegation.md`).
3. **Put each issue in one group:**
   - `approve`: it is still true, and a user or operator would hit it at the
     scale of `docs/REQUIREMENTS.md` Section 1.1, or work the owner asked for
     depends on it.
   - `close`: it duplicates another open issue (name that issue), `main`
     already fixes it (name the commit or the test), it is a problem with the
     machine (`AGENTS.md` Section 5.3), or the code it describes is gone.
   - `keep`: it is still true and not worth doing now. It stays open as a
     record.
   - `owner decides`: the right behavior is itself unclear. Give the options.
4. **Report one table per group:** the number, the title, the reason in one
   line, and whether you `measured` or `inferred` it. Under the `approve`
   table and the `close` table, print a line the owner can send back, with
   the numbers written out:

   ```text
   approve 4101 4105 4110
   close 4090 4093
   ```

   Stop here. Change nothing until the owner answers.
5. **Do what the owner answers, and nothing more.** Label the issues the
   owner named for approval:

   ```sh
   gh issue edit 4101 4105 4110 --add-label approved
   ```

   The hook lets this through only for numbers the owner typed in this
   session, so a group name is not enough: ask for the numbers. Close only
   the issues the owner named for closing, each with a comment that gives the
   reason.
6. **Read the result back:**
   `gh issue list --state open --label approved --json number,title`.

**Reply:** the number of issues in each group, what you labelled and what you
closed, and the approved list as you read it back.
