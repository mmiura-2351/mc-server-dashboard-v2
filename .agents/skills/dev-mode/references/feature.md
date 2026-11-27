# Feature

**You own the design.** Use this for new behavior and for changed behavior.

1. Trace the code the change touches, end to end, before you design anything.
2. If the change crosses a module or function boundary, sketch two different
   shapes for it: the types, the signatures, and where each piece of state
   lives. Pick the one where each piece of state has one owner and callers
   need to know least. Write the choice down in one paragraph.
3. Note which repository contracts the change touches: a `proto/` contract, a
   new Alembic migration, an api route or schema, generated code
   (`docs/dev/CONTRIBUTING.md` Section 5, `docs/dev/AGENTS.md` Section 5).
4. Build in small units. Each unit ends in a check that you run before you
   start the next one.
5. Verify each "Done when" check on the real thing: run the command, call the
   endpoint, drive the page. When that needs a running system, follow the
   runtime-check rules in `delegation.md`.
6. Reread the whole diff. Take out what the task did not ask for: changes to
   adjacent code, comments that narrate the code, guards with no observed
   failure behind them.
7. Follow `open-pr.md`.

**Reply:** what you built, the shape you chose and the one you rejected, and
the result of each "Done when" check.
