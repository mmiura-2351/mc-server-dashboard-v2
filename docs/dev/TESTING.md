# Testing Policy

v2 is developed **test-first**, following Kent Beck's Test-Driven Development.
This document fixes the *discipline and philosophy* of testing for the project.
The concrete tooling — test runners, commands, directory layout, CI wiring — is
per-component (Python `api/`, Go `worker/`, Go `relay/`, TypeScript `webui/`)
and is pinned as it lands. On mechanics, a future per-component tooling doc
wins; on discipline, this document wins.

> **Status**: the per-component test toolchains are in place and CI-enforced
> (`api/` `pytest`, `worker/` and `relay/` `go test`, `webui/` Vitest, run via
> `make check`; see `.github/workflows/`). The discipline below applies from the
> first line of code.

## 1. The cycle: Red → Green → Refactor

Every behavioral change goes through this loop:

1. **Red** — write the smallest test that expresses the next piece of behavior
   you want. Run it and watch it fail *for the reason you expect*. A test that
   passes the moment you write it has not yet earned its place.
2. **Green** — write the simplest code that makes the test pass. Speed over
   elegance here: hardcoding, duplication, and otherwise "ugly" code are allowed
   in service of reaching green quickly.
3. **Refactor** — with the bar green, remove the duplication and improve the
   design **without changing behavior**. The tests are the safety net; re-run
   them after each small step.

The rule that anchors the loop: **never write production code except to make a
failing test pass.**

## 2. Working disciplines

- **One test at a time.** Keep the red phase short — a single assertion's worth
  of new behavior.
- **Keep a test list.** Before and during a task, jot the cases you intend to
  cover: happy path, error paths, boundaries, permission checks. Write one,
  cross it off, add new ones as you discover them. The list is scratch, not a
  deliverable.
- **Reach green by the cheapest honest route:**
  - *Fake it* — return a constant, then generalize as further tests pin the
    behavior down.
  - *Triangulate* — add a second, differing example to force the general
    solution.
  - *Obvious implementation* — when the real code is trivial and certain, just
    write it.
- **Small steps over big leaps.** If tests go red unexpectedly, the step was too
  big — shrink it.

## 3. Tidy First — separate structural and behavioral change

Follow the *Tidy First?* separation: a change either **changes behavior**
(alters what the system does, driven by a new test) or **changes structure**
(refactor — same behavior, better shape). Do not mix the two in one commit. When
messy structure blocks the change you want, tidy first as its own step with
tests green, then make the behavioral change on top.

## 4. What a good test looks like

- **Tests behavior, not implementation.** Name it for the observable outcome
  (`rejects_empty_name`), not the mechanism. Behavior tests survive refactors;
  implementation tests obstruct them.
- **Fast, isolated, deterministic, repeatable.** No dependence on wall-clock
  time, execution order, network flakiness, or another test's side effects. The
  same input always yields the same result.
- **One reason to fail.** A focused assertion makes a red test point straight at
  the cause.
- **Arrange–Act–Assert.** Set up the world, exercise the one behavior under
  test, assert the outcome.
- **Tests are documentation.** A reader should learn how a unit is meant to
  behave by reading its tests.

To keep the inner loop fast, replace external dependencies (database,
filesystem, network, processes, clock) with in-memory test doubles when
unit-testing logic; verify the real adapters separately (see Section 5).

## 5. Layering and feedback loops

Favor many small, fast tests over a few slow, broad ones (the testing pyramid):

- The bulk of coverage sits in **fast unit tests** that drive the design through
  the red/green/refactor loop.
- **Fewer** tests exercise real adapters and integration points (real DB, the
  HTTP/gRPC boundary).
- The **fewest** cover end-to-end behavior across components.

Fast tests run continuously in the TDD inner loop; the slower, broader tests run
before a change is integrated. Concrete layer names, directory layout, and run
commands are per-component (see each module's `Makefile` targets and CI
workflow). All components (`api/`, `worker/`, `relay/`, `webui/`) follow the
same discipline described above.

## 6. References

- Kent Beck, *Test-Driven Development by Example* — the red/green/refactor
  discipline and the get-to-green strategies (fake it, triangulate, obvious
  implementation).
- Kent Beck, *Tidy First?* — separating structural change from behavioral
  change.
