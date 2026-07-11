"""Regression guard: error bodies stay RFC 9457 problem+json (issue #371).

Routes must raise through the central :mod:`mc_server_dashboard_api.http_problem`
mechanism, never build an ad-hoc ``HTTPException(detail=...)`` body. A bare
``detail=`` keyword anywhere under ``api/src`` (outside the central module, which
legitimately sets ``detail`` on the base exception) would reintroduce a second
error shape, so this test fails on it.

The ``detail=`` keyword is not the only way to reach an ad-hoc body: a positional
construction such as ``HTTPException(404, "msg")`` sets ``detail`` without naming
it. So this module also fails on any bare ``HTTPException(`` /
``StarletteHTTPException(`` *construction* outside the central module — while
still allowing ``import``, ``except HTTPException`` re-raises, subclassing, and
type annotations, none of which are followed by a call's opening parenthesis.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "mc_server_dashboard_api"

# The one module allowed to mention ``detail`` — it IS the central mechanism.
_ALLOWED = {_SRC / "http_problem.py"}

# Modules whose ``detail=`` is a *data field*, not an HTTP error body: the
# schedule repository maps the ``schedule_run.detail`` column (issue #1835), the
# schedule router serialises it into the run-history response (issue #1837), and
# the schedule runner + its notifier adapter carry the same sanitized
# ``schedule_run.detail`` / notification ``detail`` through the run history and
# the real-time bus (issue #1838). Exempt from the keyword scan only; the
# HTTPException-construction scan below still covers them.
_DETAIL_FIELD_ALLOWED = {
    _SRC / "servers" / "adapters" / "schedule_repository.py",
    _SRC / "servers" / "api" / "schedules.py",
    _SRC / "servers" / "adapters" / "notifier.py",
    _SRC / "servers" / "application" / "schedule_runner.py",
}

# Matches a ``detail=`` keyword argument (the HTTPException ad-hoc body shape).
# A keyword argument has no spaces around ``=`` (PEP 8 / ruff format), which
# distinguishes it from a ``detail = ...`` local-variable assignment.
_DETAIL = re.compile(r"\bdetail=(?!=)")

# Matches a bare ``HTTPException(...)`` / ``StarletteHTTPException(...)``
# construction — the opening parenthesis is what marks a call. ``except
# HTTPException:``, ``import HTTPException``, ``class Foo(StarletteHTTPException)``,
# and ``exc: StarletteHTTPException`` are never followed by ``(``, so they pass.
_CONSTRUCT = re.compile(r"\b(?:Starlette)?HTTPException\(")


def test_no_ad_hoc_detail_outside_central_module() -> None:
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path in _ALLOWED | _DETAIL_FIELD_ALLOWED:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _DETAIL.search(line):
                offenders.append(f"{path.relative_to(_SRC)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Raise errors via mc_server_dashboard_api.http_problem.problem(...), "
        "not an ad-hoc HTTPException(detail=...):\n" + "\n".join(offenders)
    )


def test_no_http_exception_construction_outside_central_module() -> None:
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path in _ALLOWED:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _CONSTRUCT.search(line):
                offenders.append(f"{path.relative_to(_SRC)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Raise errors via mc_server_dashboard_api.http_problem.problem(...), "
        "not by constructing HTTPException(...) (even positionally):\n"
        + "\n".join(offenders)
    )
