"""Regression guard: error bodies stay RFC 9457 problem+json (issue #371).

Routes must raise through the central :mod:`mc_server_dashboard_api.http_problem`
mechanism, never build an ad-hoc ``HTTPException(detail=...)`` body. A bare
``detail=`` keyword anywhere under ``api/src`` (outside the central module, which
legitimately sets ``detail`` on the base exception) would reintroduce a second
error shape, so this test fails on it.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "mc_server_dashboard_api"

# The one module allowed to mention ``detail`` — it IS the central mechanism.
_ALLOWED = {_SRC / "http_problem.py"}

# Matches a ``detail=`` keyword argument (the HTTPException ad-hoc body shape).
# A keyword argument has no spaces around ``=`` (PEP 8 / ruff format), which
# distinguishes it from a ``detail = ...`` local-variable assignment.
_DETAIL = re.compile(r"\bdetail=(?!=)")


def test_no_ad_hoc_detail_outside_central_module() -> None:
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path in _ALLOWED:
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
