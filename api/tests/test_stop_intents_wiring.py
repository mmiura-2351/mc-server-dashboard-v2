"""The composition root hands every lifecycle use case the one stop-intent count.

A start is held back from a stop only if both use the same ``StopIntentCounter``
(issue #3739). Each use case defaults to a private counter, so a construction
site that forgets the app's one fails silently: its starts and stops pass each
other unseen. ``app.py``'s lifespan closures (the schedule runner's and the
reconciler's use cases) are never built by a test, so this scan checks them, and
every other site, syntactically: each construction must pass ``stop_intents=``
from ``app.state``. ``tests/servers/test_stop_intents.py`` checks the request-
scoped sites at runtime.

The scan matches bare-name calls only (``StartServer(...)``). A qualified call
(``lifecycle.StartServer(...)``), an ``import ... as`` alias, a subclass or a
``functools.partial`` escapes it, and nothing checks the lifespan sites at
runtime behind it. ``src`` has no such construction today; keep it that way.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "mc_server_dashboard_api"

# The use cases that bump or read the count.
_USERS = {"StartServer", "StopServer", "StopServersAssignedToWorker"}

# The late-snapshot sink's StopServer runs only the late release, which neither
# bumps nor reads the count, so it keeps its private default.
_PRIVATE_BY_DESIGN = {Path("servers/adapters/late_snapshot_result_sink.py")}


def _constructions(path: Path) -> list[tuple[str, ast.Call]]:
    """Each bare-name construction of a ``_USERS`` use case, with its name."""

    return [
        (node.func.id, node)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _USERS
    ]


def _passes_the_app_counter(call: ast.Call) -> bool:
    # ``app.state.stop_intents`` or ``request.app.state.stop_intents``.
    value = next((kw.value for kw in call.keywords if kw.arg == "stop_intents"), None)
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "stop_intents"
        and isinstance(value.value, ast.Attribute)
        and value.value.attr == "state"
    )


def test_every_lifecycle_use_case_gets_the_app_stop_intent_count() -> None:
    offenders = [
        f"{path.relative_to(_SRC)}:{call.lineno}: {name}(...)"
        for path in sorted(_SRC.rglob("*.py"))
        if path.relative_to(_SRC) not in _PRIVATE_BY_DESIGN
        for name, call in _constructions(path)
        if not _passes_the_app_counter(call)
    ]
    assert not offenders, (
        "Pass the app's one counter, stop_intents=app.state.stop_intents (or "
        "request.app.state.stop_intents), so these starts and stops see each "
        "other:\n" + "\n".join(offenders)
    )


def test_the_scan_sees_the_constructions_it_guards() -> None:
    """Not vacuous: the composition root's sites, and the exempt one, are found."""

    found = {
        name
        for path in (_SRC / "app.py", _SRC / "dependencies.py")
        for name, _ in _constructions(path)
    }
    assert found == _USERS
    # The schedule runner's and the reconciler's StartServer and StopServer. A
    # new lifespan site only raises the count; fewer means the scan lost sight
    # of one (a renamed or qualified call, see the module docstring).
    lifespan_sites = len(_constructions(_SRC / "app.py"))
    assert lifespan_sites >= 4, (
        f"the scan sees {lifespan_sites} lifespan constructions in app.py, "
        "expected at least the 4 it was written against; if one moved behind "
        "a qualified name or an alias, make it a bare-name call again"
    )
    for exempt in _PRIVATE_BY_DESIGN:
        assert _constructions(_SRC / exempt), f"{exempt} no longer builds one"
