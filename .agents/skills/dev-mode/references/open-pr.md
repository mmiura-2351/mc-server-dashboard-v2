# Opening a pull request

The investigation playbook never opens one. Every other playbook that changes
files ends here.

1. Work in a git worktree off `main`, never in the repository root
   (`docs/dev/AGENTS.md` Sections 1 and 2). Run `make bootstrap` once in a
   fresh worktree.
2. Commit in small steps. Each commit lands on its own.
3. While you iterate, run the targeted checks for what you touched
   (`docs/dev/AGENTS.md` Section 5). Do not run the full `make check` by hand:
   the pre-push hook runs it once.
4. Push. When the hook fails, fix the cause in a new commit. Never pass
   `--no-verify`.
5. Open the pull request with `gh pr create`, following
   `docs/dev/CONTRIBUTING.md` Section 5: the title, exactly one category
   label, `Resolves #N` when an issue exists, and the body sections. Fill
   `## Verification` with the commands you ran and what they printed.

Opening a pull request does not start a review. Landing it is a separate step
(`land-pr.md`).

**Reply:** the pull request number and URL, the result of each "Done when"
check, and any defect or proposal you found on the way.
