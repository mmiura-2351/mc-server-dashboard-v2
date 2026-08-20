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
resolution, so on a tree that uses a construct outside that model it
**declines to judge** the head count rather than emit a verdict it cannot
stand behind (#2534): it exits clean, names the construct and the file, and
points at the authoritative ``alembic heads --resolve-dependencies`` step in
``.github/workflows/api.yml``. Checks 2 and 3 need no graph resolution and
still apply.

Two constructs trigger that decline: a non-``None`` ``depends_on`` -- alembic
resolves a dependency into a head edge, so a revision that another head depends
on is not itself a head (measured: a two-branch tree whose one tip declares
``depends_on`` on the other is one head to alembic and two here) -- and a
non-``None`` ``branch_labels``, a symbolic name alembic accepts wherever it
accepts a revision id. Both are fields this guard reads but does not resolve
into its graph, and neither appears by accident: all 36 version files set both
to ``None``, as the ``script.py.mako`` template generates them.

An unknown ``down_revision`` id is deliberately *not* a decline. Alembic
matches a ``down_revision`` edge by exact id as well (``RevisionMap`` looks the
parent up in a dict); the partial-id resolution in ``alembic/script/revision.py``
applies to command targets and to ``depends_on``, not to ``down_revision``
links. Measured against alembic 1.18.4, a ``down_revision`` naming a shortened
id or a nonexistent revision makes alembic exit non-zero, exactly as this guard
does -- and that shape is also what a typo or a deleted parent looks like, so
it stays a failure here. (A ``down_revision`` naming a branch label errors in
alembic too, but such a tree declines anyway: declaring the label is itself a
trigger.)

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

    ``depends_on`` and ``branch_labels`` are kept as written, uninterpreted:
    they are the constructs the guard declines to judge a tree by, and only
    their being non-``None`` matters (``unmodelled_constructs``).
    """

    def __init__(
        self,
        path: Path,
        revision: str,
        down_revisions: tuple[str, ...],
        depends_on: object = None,
        branch_labels: object = None,
    ):
        self.path = path
        self.revision = revision
        self.down_revisions = down_revisions
        self.depends_on = depends_on
        self.branch_labels = branch_labels


_ABSENT = object()


def _assigned_literal(
    tree: ast.Module, name: str, path: Path, default: object = _ABSENT
) -> object:
    """The value of the module-level ``name = <literal>`` assignment.

    A type annotation (``revision: str = "..."``) is optional, matching both the
    annotated form ``migrations/script.py.mako`` generates and the bare form.

    ``default`` is returned when the file has no such assignment; without it a
    missing assignment is an error. Alembic reads ``branch_labels`` and
    ``depends_on`` off the module with ``getattr``, so a version file may omit
    them; ``revision`` and ``down_revision`` it always requires.
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
    if default is not _ABSENT:
        return default
    raise ValueError(f"{path}: no `{name} = ...` assignment found")


def parse_migration(path: Path) -> Migration:
    """Parse a version file's ``revision`` / ``down_revision`` assignments.

    Reads the literals from the module's syntax tree rather than importing it,
    so this stays free of alembic, the api/ venv, and any import side effect.
    ``down_revision`` mirrors what alembic accepts (``alembic.util.to_tuple``):
    an id, ``None``, or a tuple/list of ids for a merge migration.

    ``depends_on`` and ``branch_labels`` are read but not interpreted: whether
    they are ``None`` is all this guard needs (``unmodelled_constructs``).
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

    return Migration(
        path,
        revision,
        down_revisions,
        _assigned_literal(tree, "depends_on", path, None),
        _assigned_literal(tree, "branch_labels", path, None),
    )


def _label(path: Path, label_root: Path) -> str:
    """``path`` relative to the repo root when it is inside it."""
    try:
        return str(path.relative_to(label_root))
    except ValueError:
        return str(path)


def unmodelled_constructs(migrations: list[Migration], label_root: Path) -> list[str]:
    """Constructs in the tree whose head resolution this guard does not model.

    Non-empty means the head count is not this guard's to judge: it declines
    (see the module docstring). The trigger is the *presence* of a non-``None``
    ``depends_on`` or ``branch_labels``, not its effect -- deciding the effect
    is the modelling being declined.
    """
    reasons = {
        "depends_on": (
            "alembic resolves a dependency into a head edge, so a revision that "
            "another head depends on is not itself a head"
        ),
        "branch_labels": (
            "alembic accepts a branch label wherever it accepts a revision id, "
            "including as a `depends_on`"
        ),
    }
    messages: list[str] = []
    for field, reason in reasons.items():
        declared = [m.path for m in migrations if getattr(m, field) is not None]
        if declared:
            files = ", ".join(_label(p, label_root) for p in sorted(declared))
            messages.append(f"`{field}` is declared in {files} -- {reason}")
    return messages


def check_migrations(migrations: list[Migration], label_root: Path) -> list[str]:
    """Return a list of violation messages (empty if clean).

    The head check is skipped -- not passed -- on a tree that uses a construct
    ``unmodelled_constructs`` reports; the other two checks need no graph
    resolution and always apply.
    """
    errors: list[str] = []

    def label(path: Path) -> str:
        return _label(path, label_root)

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
    # A tree that uses a construct outside this model gets no verdict at all,
    # rather than one computed from an incomplete graph (#2534).
    if unmodelled_constructs(migrations, label_root):
        return errors

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

    declines = unmodelled_constructs(migrations, repo_root)
    if declines:
        print("check-migrations: declining to judge the head count (#2534):")
        for message in declines:
            print(f"  {message}")
        print(
            "  `alembic heads --resolve-dependencies` -- the migration-guard step "
            "in .github/workflows/api.yml -- is authoritative for this tree."
        )

    errors = check_migrations(migrations, repo_root)
    if errors:
        print("check-migrations found violations:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    if declines:
        print(
            f"check-migrations: {len(migrations)} migrations, revision ids and "
            "filename prefixes unique (head count not judged)"
        )
    else:
        print(f"check-migrations: OK ({len(migrations)} migrations, single head)")
    return 0


def _self_test() -> int:
    """Exercise the checks against fixtures (no real versions/ dependency)."""
    root = Path("/repo")
    failures: list[str] = []

    def mig(
        name: str,
        revision: str,
        down: str | tuple[str, ...] | None,
        depends_on: object = None,
        branch_labels: object = None,
    ) -> Migration:
        """``down``: a parent id, a tuple of them (merge), or None (baseline)."""
        path = root / "api" / "migrations" / "versions" / name
        parents = () if down is None else (down,) if isinstance(down, str) else down
        return Migration(path, revision, parents, depends_on, branch_labels)

    def expect(
        name: str,
        migrations: list[Migration],
        *,
        flagged: bool,
        declined: bool = False,
    ) -> None:
        """Assert the verdict, and whether the guard declines to judge heads."""
        got = check_migrations(migrations, root)
        if bool(got) != flagged:
            failures.append(
                f"{name}: expected {'a violation' if flagged else 'no violation'}, "
                f"got {got!r}"
            )
        got_declines = unmodelled_constructs(migrations, root)
        if bool(got_declines) != declined:
            failures.append(
                f"{name}: expected {'a decline' if declined else 'no decline'}, "
                f"got {got_declines!r}"
            )

    # A clean linear chain: one head, unique ids, unique prefixes.
    clean = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0003_c.py", "0003_c", "0002_b"),
    ]
    expect("clean chain", clean, flagged=False)

    # Two heads: two migrations chained off the same parent (the M2 collision).
    two_heads = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0002_c.py", "0002_c", "0001_a"),
    ]
    expect("two heads", two_heads, flagged=True)

    # Duplicate revision id (same id in two files).
    dup_id = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_dup", "0001_a"),
        mig("0003_c.py", "0002_dup", "0002_dup"),
    ]
    expect("duplicate revision id", dup_id, flagged=True)

    # Duplicate numeric filename prefix (0002 twice) with distinct chained ids.
    dup_prefix = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0002_c.py", "0002_c", "0002_b"),
    ]
    expect("duplicate filename prefix", dup_prefix, flagged=True)

    # A merge migration reconciles two heads: both parents are consumed, so the
    # merge is the single head (this is what alembic reports for such a tree).
    merged = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0003_c.py", "0003_c", "0001_a"),
        mig("0004_m.py", "0004_m", ("0002_b", "0003_c")),
    ]
    expect("merge migration", merged, flagged=False)

    # A merge that consumes only one of the two heads leaves the other a head.
    partial_merge = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0003_c.py", "0003_c", "0001_a"),
        mig("0004_m.py", "0004_m", "0002_b"),
    ]
    expect("partial merge", partial_merge, flagged=True)

    # `depends_on` on one of two branch tips: alembic resolves the dependency
    # into a head edge and reports one head, this guard's parent set does not
    # model that, so it declines instead of reporting the phantom second head
    # (#2534; measured against alembic 1.18.4).
    depends_on_tree = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0003_c.py", "0003_c", "0001_a", depends_on="0002_b"),
    ]
    expect("depends_on tree", depends_on_tree, flagged=False, declined=True)

    # The decline is on the construct's presence, not on its effect: a
    # dependency that changes no head is not modelled either, and judging it
    # would mean modelling it.
    inert_depends_on = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0003_c.py", "0003_c", "0002_b", depends_on="0001_a"),
    ]
    expect("inert depends_on", inert_depends_on, flagged=False, declined=True)

    # `branch_labels` names a revision symbolically wherever alembic accepts an
    # id -- another construct outside this guard's model.
    labelled = [
        mig("0001_a.py", "0001_a", None, branch_labels="tip"),
        mig("0002_b.py", "0002_b", "0001_a"),
    ]
    expect("branch_labels", labelled, flagged=False, declined=True)

    # An unknown parent id is NOT a decline: alembic matches a `down_revision`
    # edge by exact id too (`RevisionMap._revision_map`) and exits non-zero on
    # a tree like this, so the guard keeps failing rather than excusing what a
    # typo or a deleted parent looks like.
    typo_parent = [
        mig("0001_a.py", "0001_aaaaaaaa", None),
        mig("0002_b.py", "0002_bbbbbbbb", "0001_zzz"),
    ]
    expect("nonexistent parent", typo_parent, flagged=True, declined=False)

    # Same for a shortened parent id: alembic's partial-id resolution applies to
    # command targets and `depends_on`, not to `down_revision` links -- measured
    # on alembic 1.18.4, where this tree is an error, not one head.
    shortened_parent = [
        mig("0001_a.py", "0001_aaaaaaaa", None),
        mig("0002_b.py", "0002_bbbbbbbb", "0001_aaa"),
    ]
    expect("shortened parent id", shortened_parent, flagged=True, declined=False)

    # Declining is scoped to the head count: the checks that need no graph
    # resolution still report, so this tree both declines and fails.
    declined_and_flagged = [
        mig("0001_a.py", "0001_a", None),
        mig("0002_b.py", "0002_b", "0001_a"),
        mig("0002_c.py", "0002_c", "0002_b", depends_on="0001_a"),
    ]
    expect(
        "decline with a duplicate prefix",
        declined_and_flagged,
        flagged=True,
        declined=True,
    )

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

        # `branch_labels` / `depends_on` decide whether the guard declines, so
        # each shape has to read back as written. Alembic takes both as optional
        # module attributes (`Script._from_path` reads them with getattr), so a
        # file that omits them is legal and parses as None -- as does the
        # explicit `None` every file in versions/ carries.
        def expect_constructs(
            name: str, body: str, want: tuple[object, object]
        ) -> None:
            migration = parsed(name, body)
            if migration is None:
                return
            got = (migration.depends_on, migration.branch_labels)
            if got != want:
                failures.append(
                    f"{name}: expected (depends_on, branch_labels) {want!r}, "
                    f"got {got!r}"
                )

        def full_template(depends_on: str, branch_labels: str) -> str:
            """All four assignments ``migrations/script.py.mako`` generates."""
            return (
                'revision: str = "0004_m"\n'
                'down_revision: str | Sequence[str] | None = "0003_c"\n'
                f"branch_labels: str | Sequence[str] | None = {branch_labels}\n"
                f"depends_on: str | Sequence[str] | None = {depends_on}\n"
            )

        expect_constructs("parse constructs absent", annotated("None"), (None, None))
        expect_constructs(
            "parse constructs None", full_template("None", "None"), (None, None)
        )
        expect_constructs(
            "parse constructs declared",
            full_template('"0002_b"', '("tip",)'),
            ("0002_b", ("tip",)),
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
