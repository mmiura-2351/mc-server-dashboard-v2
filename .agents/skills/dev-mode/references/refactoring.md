# Refactoring

**You own the contract. The structure changes. The behavior does not.**

1. Pin the current behavior before any structure moves. Find the tests that
   cover it. If none do, write a characterization test or record the current
   output. A type check and a lint run are not a pin.
2. Name the target shape: what the modules, the types, and the call graph
   would be if this code were written today.
3. Remove before you add. Delete dead code, inline wrappers that have one
   caller, and drop validators that repeat a check made at the boundary.
4. Move in small steps, and keep the pin green after each one. When an
   internal API changes shape, migrate every caller and delete the old path in
   the same pull request. Do not leave the old and the new path side by side.
5. If you find a bug or a missing feature on the way, do not fix it here.
   Report it (`AGENTS.md` Section 5.3).
6. Check that the change is worth keeping: a reader now has fewer layers to
   trace or less state to hold in mind. If not, revert it.
7. Follow `open-pr.md`. Structural commits and behavioral commits stay
   separate (`docs/dev/TESTING.md` Section 3).

**Reply:** the structure that changed, the pin you held it against, the proof
that behavior is unchanged, and what you reverted.
