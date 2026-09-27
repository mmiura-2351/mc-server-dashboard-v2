"""Unit tests for the xdist-controller guard on scratch-DB creation (issue #1742).

Under ``pytest -n``, ``pytest_configure`` runs once on the xdist *controller*
and once per *worker*. Only workers run tests, so a scratch database created on
the controller is never used — it is created and immediately dropped again. The
:func:`tests.conftest._is_xdist_controller` predicate lets ``pytest_configure``
skip creation on the controller while still creating it for workers and for
serial (no ``-n``) runs. These tests pin those states, including the conservative
fallback when xdist did not register its options.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from tests.conftest import _is_xdist_controller


def _config(*, dist: str | None = "no", worker: bool = False) -> pytest.Config:
    """A minimal stand-in for ``pytest.Config``.

    Mirrors exactly the two attributes the predicate reads: ``config.option.dist``
    (present only when xdist registered its options) and ``config.workerinput``
    (present only on an xdist worker).
    """
    option = SimpleNamespace() if dist is None else SimpleNamespace(dist=dist)
    config = SimpleNamespace(option=option)
    if worker:
        config.workerinput = {"workerid": "gw0"}
    return cast(pytest.Config, config)


@pytest.mark.parametrize(
    ("dist", "worker", "expected"),
    [
        pytest.param("load", False, True, id="xdist-controller"),
        pytest.param("load", True, False, id="xdist-worker"),
        pytest.param("no", False, False, id="serial-run"),
        # If xdist did not register its options, create the DB rather than risk an
        # unsafe shared-database run by treating the process as a controller.
        pytest.param(None, False, False, id="missing-xdist-options"),
    ],
)
def test_xdist_controller_detection(
    dist: str | None, worker: bool, expected: bool
) -> None:
    assert _is_xdist_controller(_config(dist=dist, worker=worker)) is expected
