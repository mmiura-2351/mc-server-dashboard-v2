"""``migrations/script.py.mako`` must generate migrations that type-check.

``alembic merge`` is the canonical way to reconcile the two heads parallel PRs
create (CONTRIBUTING.md Section 5), and it assigns a **tuple** of parent ids to
``down_revision``. ``make check`` type-checks the version files (``cd api && uv
run mypy .`` collects ``migrations/versions/*.py``), so a template annotation
that admits only ``str | None`` turns a correctly generated merge migration into
a red gate -- with a message about type compatibility rather than about
migrations (#2535).

The check **generates** rather than reads the template: it renders the real
``migrations/script.py.mako`` through alembic into a throwaway version tree and
runs mypy with the api/ config over the result. That pins the property the gate
enforces instead of the spelling of one annotation, and it keeps working if a
future alembic changes what it feeds the template.

mypy runs over the whole generated tree rather than the merge file alone, so the
narrow shapes stay covered too: widening an annotation can in principle break the
cases that used to fit it, and nothing else checks what the template renders for
a root or for an ordinary child.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

_API_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _API_ROOT / "migrations" / "script.py.mako"


def _generate_version_tree(tmp_path: Path) -> Path:
    """Render the repo's template into every ``down_revision`` shape alembic writes.

    A root (``None``), two migrations chained off it the way parallel PRs collide
    (a scalar parent id each), and the ``alembic merge`` reconciling them (a tuple
    of parent ids). Returns the ``versions/`` directory holding all four.
    """
    script_location = tmp_path / "migrations"
    (script_location / "versions").mkdir(parents=True)
    shutil.copy(_TEMPLATE, script_location / "script.py.mako")

    config = Config()
    config.set_main_option("script_location", str(script_location))
    command.revision(config, message="root", rev_id="root", head="base")
    command.revision(config, message="branch a", rev_id="branch_a", head="root")
    command.revision(
        config, message="branch b", rev_id="branch_b", head="root", splice=True
    )
    command.merge(
        config,
        revisions=("branch_a", "branch_b"),
        message="merge branches",
        rev_id="merge_heads",
    )

    return script_location / "versions"


def test_merge_migration_assigns_a_tuple_of_parents(tmp_path: Path) -> None:
    """The type-check below is only interesting while alembic still writes a tuple."""
    (merge,) = _generate_version_tree(tmp_path).glob("merge_heads_*.py")

    (assigned,) = [
        node.value
        for node in ast.parse(merge.read_text(encoding="utf-8")).body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "down_revision"
        and node.value is not None
    ]

    assert ast.literal_eval(assigned) == ("branch_a", "branch_b")


def test_generated_migrations_pass_mypy(tmp_path: Path) -> None:
    versions = _generate_version_tree(tmp_path)

    # mypy deliberately shares api/.mypy_cache (cwd is the api root and no
    # --cache-dir is passed): warm that is ~0.3s, while an isolated cache pays a
    # ~5s cold analysis of alembic and sqlalchemy on every run. It is safe only
    # because this is the only test that invokes mypy and check_parallel.sh runs
    # chain A as `api-lint -> api-test` serially, so nothing else writes the
    # cache concurrently. A second mypy-invoking test has to re-check that.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(_API_ROOT / "pyproject.toml"),
            str(versions),
        ],
        cwd=_API_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
