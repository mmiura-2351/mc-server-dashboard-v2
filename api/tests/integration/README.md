# Integration tests

These tests exercise the real adapters against a live PostgreSQL database (the
HTTP/DB boundary in the testing pyramid; see
[`docs/dev/TESTING.md`](../../../docs/dev/TESTING.md) Section 5). They run only
when `MCD_TEST_DATABASE_URL` is set; otherwise pytest skips them and the unit
suite stays hermetic.

## `MCD_TEST_DATABASE_URL` is a *base* connection

The variable is the **maintenance/base** connection, not the database the tests
run against. At session start (`tests/conftest.py:pytest_configure`) the suite:

1. creates a fresh `<dbname>_<short-uuid>` database off the base URL,
2. repoints `MCD_TEST_DATABASE_URL` at it for the rest of the session, and
3. drops it on teardown (best-effort, with `WITH (FORCE)`).

This makes parallel runs that share one base URL — e.g. two agent worktrees
pointed at the same local Postgres — collide-proof: each session owns a disjoint
database, so the `downgrade base` / `upgrade head` dance in the fixtures can no
longer race or leave orphan tables behind (issue #379). The base user must be
able to `CREATE DATABASE`.

CI is unaffected: it provisions a fresh Postgres service per run, and the per-run
database derived from that service's URL is just as fresh.

## Known limitation: cluster-level contention under concurrent runs

The per-session scratch database removes *name collisions* and orphan tables, so
two sessions never touch the same data. It does **not** isolate the shared
Postgres *cluster's* resources (connection slots, shared buffers, disk IO). When
several full suites hammer one local cluster at once, the DB-gated integration
tests — most visibly `test_lifecycle_repositories` / `test_lifecycle_scenarios`
— can throw transient errors that vanish when run in isolation (issue #1373).

This is inherent to sharing one cluster across concurrent runs; a proper fix
(a dedicated cluster per run, or cross-process serialization the Makefile cannot
coordinate) is out of scope here and deliberately not built. If you hit it
locally, point the overlapping run at its own Postgres (a second container on a
different port) or re-run the affected test. CI is unaffected — each run gets its
own Postgres service.

## Running locally

Point at a *scratch* Postgres — never the live deployment database. For example:

```sh
docker run --rm -d --name mcd-test-pg -p 5544:5432 \
  -e POSTGRES_USER=mcsd -e POSTGRES_PASSWORD=mcsd -e POSTGRES_DB=mcsd_test \
  postgres:17-alpine

cd api
MCD_TEST_DATABASE_URL="postgresql+asyncpg://mcsd:mcsd@localhost:5544/mcsd_test" \
  uv run pytest tests/integration
```
