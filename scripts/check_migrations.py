#!/usr/bin/env python3
"""Guard against migration numbering collisions between parallel PRs.

Parallel PRs each chain a migration off the same ``main`` head; each PR's CI is
green in isolation, and the collision only surfaces when the first one merges
(issue #284). This check runs on the ``pull_request`` *merge ref* (PR combined
with current ``origin/main`` -- the default for ``actions/checkout``), so it
catches the collision the moment ``main`` moves, at the next push or re-run.

It scans ``api/migrations/versions/*.py`` (pure file parsing -- no DB, no
alembic import) and fails loudly, naming the offending files, for each
violation:

1. **Single head.** Exactly one revision must be a head (i.e. nobody's
   ``down_revision``). Two heads means two migrations chained off the same
   parent -- the parallel-PR collision.
2. **Unique revision ids.** No two files may declare the same ``revision``.
3. **Unique filename prefixes.** No two files may share the same numeric
   ``NNNN_`` prefix (the human-facing ordering that collided three times in M2).

The DB-gated metadata-sync test covers chain validity, but only on the merge
ref and only when CI actually runs; this fast non-DB step makes the head/number
invariants explicit and self-tested.

Pure standard library; runs under any Python 3.8+ (the api/ venv or a system
python). Exit status is non-zero when any check fails.

Run ``scripts/check_migrations.py --self-test`` to exercise the checks against
in-memory fixtures (the helpers, not the real versions/ tree).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# A module-level ``revision = "..."`` / ``down_revision = "..."`` assignment. The
# value may be a quoted id or ``None`` (the baseline's ``down_revision``); a type
# annotation (``revision: str = "..."``) is optional.
REVISION = re.compile(r'^revision(?:\s*:[^=]+)?\s*=\s*["\']([^"\']+)["\']', re.M)
DOWN_REVISION = re.compile(
    r'^down_revision(?:\s*:[^=]+)?\s*=\s*(?:["\']([^"\']+)["\']|None)', re.M
)

# The numeric ordering prefix of a version filename (e.g. ``0011`` in
# ``0011_user_active.py``).
FILENAME_PREFIX = re.compile(r"^(\d+)_")


class Migration:
    """A parsed migration: its file, revision id, and down_revision (or None)."""

    def __init__(self, path: Path, revision: str, down_revision: str | None):
        self.path = path
        self.revision = revision
        self.down_revision = down_revision


def parse_migration(path: Path) -> Migration:
    text = path.read_text(encoding="utf-8")
    rev_match = REVISION.search(text)
    if rev_match is None:
        raise ValueError(f"{path}: no `revision = ...` assignment found")
    down_match = DOWN_REVISION.search(text)
    if down_match is None:
        raise ValueError(f"{path}: no `down_revision = ...` assignment found")
    return Migration(path, rev_match.group(1), down_match.group(1))


def check_migrations(migrations: list[Migration], label_root: Path) -> list[str]:
    """Return a list of violation messages (empty if clean)."""
    errors: list[str] = []

    def label(path: Path) -> str:
        try:
            return str(path.relative_to(label_root))
        except ValueError:
            return str(path)

    # 2. Unique revision ids.
    by_revision: dict[str, list[Path]] = {}
    for m in migrations:
        by_revision.setdefault(m.revision, []).append(m.path)
    for revision, paths in sorted(by_revision.items()):
        if len(paths) > 1:
            files = ", ".join(label(p) for p in sorted(paths))
            errors.append(f"duplicate revision id {revision!r}: {files}")

    # 3. Unique numeric filename prefixes.
    by_prefix: dict[str, list[Path]] = {}
    for m in migrations:
        prefix_match = FILENAME_PREFIX.match(m.path.name)
        if prefix_match is None:
            errors.append(
                f"{label(m.path)}: filename has no numeric `NNNN_` ordering prefix"
            )
            continue
        by_prefix.setdefault(prefix_match.group(1), []).append(m.path)
    for prefix, paths in sorted(by_prefix.items()):
        if len(paths) > 1:
            files = ", ".join(label(p) for p in sorted(paths))
            errors.append(f"duplicate filename prefix {prefix!r}: {files}")

    # 1. Single head: every revision that is nobody's down_revision is a head.
    parents = {m.down_revision for m in migrations if m.down_revision is not None}
    heads = sorted(m.revision for m in migrations if m.revision not in parents)
    if migrations and len(heads) != 1:
        errors.append(
            f"expected exactly one migration head, found {len(heads)}: "
            f"{', '.join(heads) or '(none)'} -- parallel PRs likely chained off "
            "the same parent (renumber to main's current head; see "
            "docs/dev/CONTRIBUTING.md)"
        )

    return errors


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    versions_dir = repo_root / "api" / "migrations" / "versions"
    if not versions_dir.is_dir():
        print(f"migrations versions/ not found at {versions_dir}", file=sys.stderr)
        return 2

    try:
        migrations = [
            parse_migration(path) for path in sorted(versions_dir.glob("*.py"))
        ]
    except ValueError as exc:
        print(f"check-migrations failed to parse: {exc}", file=sys.stderr)
        return 2

    errors = check_migrations(migrations, repo_root)
    if errors:
        print("check-migrations found violations:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    print(f"check-migrations: OK ({len(migrations)} migrations, single head)")
    return 0


def _self_test() -> int:
    """Exercise the checks against in-memory fixtures (no versions/ dependency)."""
    root = Path("/repo")
    failures: list[str] = []

    def mig(name: str, revision: str, down: str | None) -> Migration:
        path = root / "api" / "migrations" / "versions" / name
        return Migration(path, revision, down)

    def expect(name: str, got: list[str], should_flag: bool) -> None:
        flagged = bool(got)
        if flagged != should_flag:
            failures.append(
                f"{name}: expected {'a violation' if should_flag else 'no violation'}, "
                f"got {got!r}"
            )

    # A clean linear chain: one head, unique ids, unique prefixes.
    clean = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0003_c.py", "0003_c", "0002_b"),
    ]
    expect("clean chain", check_migrations(clean, root), False)

    # Two heads: two migrations chained off the same parent (the M2 collision).
    two_heads = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0002_c.py", "0002_c", "0001_a"),
    ]
    expect("two heads", check_migrations(two_heads, root), True)

    # Duplicate revision id (same id in two files).
    dup_id = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_dup", "0001_a"),
        mig("0003_c.py", "0002_dup", "0002_dup"),
    ]
    expect("duplicate revision id", check_migrations(dup_id, root), True)

    # Duplicate numeric filename prefix (0002 twice) with distinct chained ids.
    dup_prefix = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0002_c.py", "0002_c", "0002_b"),
    ]
    expect("duplicate filename prefix", check_migrations(dup_prefix, root), True)

    if failures:
        print("check_migrations --self-test FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("check_migrations --self-test: OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit(main())
