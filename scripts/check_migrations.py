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
   parent -- the parallel-PR collision. A merge migration names every head it
   reconciles in a ``down_revision`` tuple, so it consumes all of them and the
   tree is back to one head.
2. **Unique revision ids.** No two files may declare the same ``revision``.
3. **Unique filename prefixes.** No two files may share the same numeric
   ``NNNN_`` prefix (the human-facing ordering that collided three times in M2).

The head count agrees with ``alembic heads --resolve-dependencies``, which CI
runs alongside this script, on any tree whose edges are ``down_revision`` --
including merges. It is **not** a general reimplementation of alembic's head
resolution: it ignores ``depends_on``, which alembic also resolves into head
edges, and it matches parents by exact revision id where alembic also accepts
partial ids and branch-label references. A tree using any of those can report
more heads here than alembic reports -- fail-safe (a false alarm, never a
missed collision), and unreachable while no version file uses them, but a real
divergence rather than a hypothetical one (#2534).

The DB-gated metadata-sync test covers chain validity, but only on the merge
ref and only when CI actually runs; this fast non-DB step makes the head/number
invariants explicit and self-tested.

Pure standard library; runs under any Python 3.8+ (the api/ venv or a system
python). Exit status is non-zero when any check fails.

Run ``scripts/check_migrations.py --self-test`` to exercise the checks against
fixtures (the helpers, not the real versions/ tree).
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path

# The numeric ordering prefix of a version filename (e.g. ``0011`` in
# ``0011_user_active.py``).
FILENAME_PREFIX = re.compile(r"^(\d+)_")


class Migration:
    """A parsed migration: its file, revision id, and parent revision ids.

    ``down_revisions`` is empty for the baseline, holds one id for a normal
    migration, and holds two or more for a merge migration (``alembic merge``,
    the canonical way to reconcile two heads).
    """

    def __init__(self, path: Path, revision: str, down_revisions: tuple[str, ...]):
        self.path = path
        self.revision = revision
        self.down_revisions = down_revisions


def _assigned_literal(tree: ast.Module, name: str, path: Path) -> object:
    """The value of the module-level ``name = <literal>`` assignment.

    A type annotation (``revision: str = "..."``) is optional, matching both the
    annotated form ``migrations/script.py.mako`` generates and the bare form.
    """
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets: list[ast.expr] = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if node.value is None or not any(
            isinstance(t, ast.Name) and t.id == name for t in targets
        ):
            continue
        try:
            return ast.literal_eval(node.value)
        except ValueError as exc:
            raise ValueError(f"{path}: `{name} = ...` is not a literal") from exc
    raise ValueError(f"{path}: no `{name} = ...` assignment found")


def parse_migration(path: Path) -> Migration:
    """Parse a version file's ``revision`` / ``down_revision`` assignments.

    Reads the literals from the module's syntax tree rather than importing it,
    so this stays free of alembic, the api/ venv, and any import side effect.
    ``down_revision`` mirrors what alembic accepts (``alembic.util.to_tuple``):
    an id, ``None``, or a tuple/list of ids for a merge migration.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    revision = _assigned_literal(tree, "revision", path)
    if not isinstance(revision, str):
        raise ValueError(f"{path}: `revision` is not a revision id: {revision!r}")

    down = _assigned_literal(tree, "down_revision", path)
    if down is None:
        down_revisions: tuple[str, ...] = ()
    elif isinstance(down, str):
        down_revisions = (down,)
    elif isinstance(down, (tuple, list)) and all(isinstance(d, str) for d in down):
        down_revisions = tuple(down)
    else:
        raise ValueError(
            f"{path}: `down_revision` is not an id, None, or a tuple of ids: {down!r}"
        )

    return Migration(path, revision, down_revisions)


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

    # 1. Single head: every revision that is nobody's down_revision is a head. A
    # merge migration names every head it reconciles, so all of them are
    # consumed -- counting only the first would leave a phantom extra head.
    # ``depends_on`` edges are deliberately not tracked; see the module
    # docstring for where that diverges from alembic (#2534).
    parents = {parent for m in migrations for parent in m.down_revisions}
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
    except SyntaxError as exc:
        # SyntaxError's own str() abbreviates the filename to its basename;
        # rebuild the message so it names the file the way the ValueError paths
        # (and every other message here) do.
        print(
            f"check-migrations failed to parse: {exc.filename}:{exc.lineno}: {exc.msg}",
            file=sys.stderr,
        )
        return 2
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
    """Exercise the checks against fixtures (no real versions/ dependency)."""
    root = Path("/repo")
    failures: list[str] = []

    def mig(name: str, revision: str, down: str | tuple[str, ...] | None) -> Migration:
        """``down``: a parent id, a tuple of them (merge), or None (baseline)."""
        path = root / "api" / "migrations" / "versions" / name
        parents = () if down is None else (down,) if isinstance(down, str) else down
        return Migration(path, revision, parents)

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

    # A merge migration reconciles two heads: both parents are consumed, so the
    # merge is the single head (this is what alembic reports for such a tree).
    merged = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0003_c.py", "0003_c", "0001_a"),
        mig("0004_m.py", "0004_m", ("0002_b", "0003_c")),
    ]
    expect("merge migration", check_migrations(merged, root), False)

    # A merge that consumes only one of the two heads leaves the other a head.
    partial_merge = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0003_c.py", "0003_c", "0001_a"),
        mig("0004_m.py", "0004_m", "0002_b"),
    ]
    expect("partial merge", check_migrations(partial_merge, root), True)

    # Parsing: the assignment forms alembic accepts. Everything but the merge
    # tuple is what the real versions/ tree already exercises; a merge migration
    # is the form no file in the tree has yet (issue #2530).
    with tempfile.TemporaryDirectory() as tmp:
        version_file = Path(tmp) / "0004_m.py"

        def parsed(name: str, body: str) -> Migration | None:
            """Parse ``body``; a parse failure is collected like any other."""
            version_file.write_text(body, encoding="utf-8")
            try:
                return parse_migration(version_file)
            except (ValueError, SyntaxError) as exc:
                failures.append(f"{name}: parse failed: {exc}")
                return None

        def annotated(down: str) -> str:
            """The assignment pair ``migrations/script.py.mako`` generates."""
            return f'revision: str = "0004_m"\ndown_revision: str | None = {down}\n'

        def expect_parents(name: str, body: str, want: tuple[str, ...]) -> None:
            migration = parsed(name, body)
            if migration is not None and migration.down_revisions != want:
                failures.append(
                    f"{name}: expected parents {want!r}, "
                    f"got {migration.down_revisions!r}"
                )

        expect_parents("parse baseline", annotated("None"), ())
        expect_parents("parse linear", annotated('"0003_c"'), ("0003_c",))
        merge_parents = ("0002_b", "0003_c")
        expect_parents(
            "parse merge tuple", annotated('("0002_b", "0003_c")'), merge_parents
        )
        # ruff wraps the tuple once the ids push the line past the line length,
        # so the wrapped form is not optional -- it is what a merge of two
        # descriptively named revisions gets formatted into.
        expect_parents(
            "parse merge tuple wrapped",
            annotated('(\n    "0002_b",\n    "0003_c",\n)'),
            merge_parents,
        )
        expect_parents(
            "parse merge list", annotated('["0002_b", "0003_c"]'), merge_parents
        )
        # Unannotated assignments (alembic's stock template) parse the same.
        expect_parents(
            "parse unannotated",
            'revision = "0004_m"\ndown_revision = ("0002_b", "0003_c")\n',
            merge_parents,
        )

        # The revision id is always a scalar (alembic never tuples it), from
        # either the annotated or the bare assignment form.
        for form, body in (
            ("annotated", annotated("None")),
            ("unannotated", 'revision = "0004_m"\ndown_revision = None\n'),
        ):
            name = f"parse revision ({form})"
            migration = parsed(name, body)
            if migration is not None and migration.revision != "0004_m":
                failures.append(
                    f"{name}: expected '0004_m', got {migration.revision!r}"
                )

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
