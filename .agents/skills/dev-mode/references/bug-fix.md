# Bug fix

**You own the fix and its proof.** Every line you ship traces to something you
observed.

1. Reproduce the bug first, on the surface the report describes: the test, the
   command, the HTTP request, or the browser flow. If it does not reproduce,
   switch to `investigation.md` and stop here.
2. Find the cause. List the candidate causes, then rule them out with evidence
   until one is left. Fix the place where the system first went wrong, not the
   place where the symptom showed.
3. Decide whether the state that triggered the bug is legitimate. If it is,
   handle it where the code broke. If it should never exist, a guard there
   hides the bug: find what produced the state.
4. Turn the reproduction into a failing test when a cheap test path exists
   (`docs/dev/TESTING.md` Section 6). Otherwise keep the reproduction command.
5. Make the smallest change that fixes the cause. A change that "might help"
   is a hypothesis. If the evidence does not need it, take it out.
6. Run the reproduction again on the same surface. It must pass. Then run the
   targeted checks for the module (`docs/dev/AGENTS.md` Section 5).
7. Follow `open-pr.md`.

**Reply:** what was broken, the cause, the fix, and the failing-then-passing
output of the reproduction, pasted as it printed.
