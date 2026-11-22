"""The schedule runner bounds a held backup's deferral by the reconciler's grace.

``app.py`` builds the runner inside the lifespan, which no test runs, so this
checks the construction syntactically (issue #4047). A shorter bound, such as
``held_start_grace_seconds``, would skip a backup while an ordinary final
snapshot is still in flight.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "mc_server_dashboard_api"
    / "app.py"
)


def test_the_runner_defers_a_held_backup_for_the_reconciler_grace() -> None:
    constructions = [
        node
        for node in ast.walk(ast.parse(_APP.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RunScheduleTick"
    ]
    assert len(constructions) == 1
    bound = next(
        kw.value for kw in constructions[0].keywords if kw.arg == "deferral_bound"
    )
    assert (
        ast.unparse(bound) == "dt.timedelta(seconds=settings.reconciler.grace_seconds)"
    )
