"""The lifespan's starts wait in the data plane's one count of uploads.

A start waits for its Worker's snapshot uploads only if it reads the same
``SnapshotUploads`` the data plane counts them in (issue #3950). A construction
handed a private instance fails silently: its starts never wait. ``app.py``'s
lifespan closures (the schedule runner's and the reconciler's starts) are never
built by a test, so this scan checks them syntactically: each
``StorageGenerationReader(...)`` there must pass
``uploads=app.state.snapshot_uploads``. The request-scoped start in
``dependencies.py`` is checked at runtime by ``tests/servers/test_stop_intents.py``.

As in ``test_stop_intents_wiring.py``, the scan matches bare-name calls only.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "mc_server_dashboard_api"
_APP = _SRC / "app.py"


def _readers() -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(_APP.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "StorageGenerationReader"
    ]


def _passes_the_app_count(call: ast.Call) -> bool:
    value = next((kw.value for kw in call.keywords if kw.arg == "uploads"), None)
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "snapshot_uploads"
        and isinstance(value.value, ast.Attribute)
        and value.value.attr == "state"
        and isinstance(value.value.value, ast.Name)
        and value.value.value.id == "app"
    )


def test_every_lifespan_store_reader_gets_the_app_upload_count() -> None:
    offenders = [
        f"app.py:{call.lineno}: StorageGenerationReader(...)"
        for call in _readers()
        if not _passes_the_app_count(call)
    ]
    assert not offenders, (
        "Pass the app's one count, uploads=app.state.snapshot_uploads, so these "
        "starts wait for the uploads the data plane counts:\n" + "\n".join(offenders)
    )


def test_the_scan_sees_the_lifespan_readers() -> None:
    # The schedule runner's start, the reconciler's start and the reconciler's own
    # reader. A new site only raises the count; fewer means the scan lost sight of
    # one (a renamed or qualified call).
    assert len(_readers()) >= 3
