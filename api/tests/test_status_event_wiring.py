"""Early warning: the composition root wires observed-state writers to the bus.

Every ``observed_state`` write stages its status event in the servers
repository, and the unit of work publishes it after the commit -- on the bus it
was built with (issue #3212). The guarantee is enforced at runtime: a unit of
work built without the bus refuses to commit such a write
(``StatusEventsNotWiredError``, pinned in
``tests/integration/test_observed_state_status_events.py``).

This scan adds only what that check cannot give: the composition root's
constructions (``app.py``'s lifespan closures above all) are never committed
through by a test, so an unwired one there would first fail in a running API,
on the first convergence write. The scan reports it at test time instead. It is
syntactic and deliberately modest -- the ``uow=`` argument of the known writers
must be a unit-of-work construction with a second argument that is not a
literal ``None`` -- so it is a convenience over the runtime check, not a
substitute for it: an alias, or a writer missing from ``_WRITERS``, escapes it
and is caught at runtime.
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
            and len(bus := [*uow.args, *(kw.value for kw in uow.keywords)]) >= 2
            and not (isinstance(bus[1], ast.Constant) and bus[1].value is None)
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
