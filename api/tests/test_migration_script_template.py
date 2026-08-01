"""``migrations/script.py.mako`` must generate a merge migration that type-checks.

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
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

_API_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _API_ROOT / "migrations" / "script.py.mako"


def _generate_merge_migration(tmp_path: Path) -> Path:
    """Render the repo's template into two heads plus the merge reconciling them."""
    script_location = tmp_path / "migrations"
    (script_location / "versions").mkdir(parents=True)
    shutil.copy(_TEMPLATE, script_location / "script.py.mako")

    config = Config()
    config.set_main_option("script_location", str(script_location))
    command.revision(config, message="branch a", rev_id="branch_a", head="base")
    command.revision(config, message="branch b", rev_id="branch_b", head="base")
    command.merge(
        config,
        revisions=("branch_a", "branch_b"),
        message="merge branches",
        rev_id="merge_heads",
    )

    (merge,) = script_location.glob("versions/merge_heads_*.py")
    return merge


def test_merge_migration_assigns_a_tuple_of_parents(tmp_path: Path) -> None:
    """The tuple form is what makes the type-check below non-vacuous."""
    merge = _generate_merge_migration(tmp_path)

    assert "down_revision: " in merge.read_text(encoding="utf-8")
    assert "= ('branch_a', 'branch_b')" in merge.read_text(encoding="utf-8")


def test_generated_merge_migration_passes_mypy(tmp_path: Path) -> None:
    merge = _generate_merge_migration(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(_API_ROOT / "pyproject.toml"),
            str(merge),
        ],
        cwd=_API_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
