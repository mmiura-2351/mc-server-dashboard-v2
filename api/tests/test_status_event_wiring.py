"""Regression guard: observed-state writers are wired to the event bus (issue #3212).

Every ``observed_state`` write stages its status event in the servers
repository, and the unit of work publishes it after the commit -- on the bus it
was built with. A unit of work built without one drops the event, which is
right for a tool with no subscribers and wrong, silently, for a use case that
writes observed state: its commit would again leave connected clients on the
old state until their next snapshot.

So every construction, under ``api/src``, of a use case that writes
``observed_state`` must hand its unit of work the bus. The scan is syntactic:
the ``uow=`` argument is a unit-of-work construction with the bus as its second
argument. A new use case that writes observed state belongs in ``_WRITERS``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "mc_server_dashboard_api"

# The use cases that write ``observed_state`` through their unit of work.
_WRITERS = {"StartServer", "StopServer", "ResetUnverifiableObservedStates"}

# The names the servers unit of work is constructed under.
_UNITS_OF_WORK = {"ServersUnitOfWork", "SqlAlchemyUnitOfWork"}


def _unwired_constructions(path: Path) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _WRITERS
        ):
            continue
        uow = next((kw.value for kw in node.keywords if kw.arg == "uow"), None)
        wired = (
            isinstance(uow, ast.Call)
            and isinstance(uow.func, ast.Name)
            and uow.func.id in _UNITS_OF_WORK
            and len(uow.args) + len(uow.keywords) >= 2
        )
        if not wired:
            offenders.append(
                f"{path.relative_to(_SRC)}:{node.lineno}: {node.func.id}(...)"
            )
    return offenders


def test_observed_state_writers_get_a_unit_of_work_wired_to_the_bus() -> None:
    offenders = [
        offender
        for path in sorted(_SRC.rglob("*.py"))
        for offender in _unwired_constructions(path)
    ]
    assert not offenders, (
        "Build the unit of work with the real-time event bus, e.g. "
        "ServersUnitOfWork(session_factory, real_time_events), so the "
        "observed-state writes of these use cases publish their status frame:\n"
        + "\n".join(offenders)
    )


def test_the_scan_sees_the_constructions_it_guards() -> None:
    """The guard is not vacuous: the composition root's constructions are found."""

    found = {
        node.func.id
        for path in (_SRC / "app.py", _SRC / "dependencies.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _WRITERS
    }
    assert found == _WRITERS
