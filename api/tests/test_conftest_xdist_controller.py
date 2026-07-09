"""Unit tests for the xdist-controller guard on scratch-DB creation (issue #1742).

Under ``pytest -n``, ``pytest_configure`` runs once on the xdist *controller*
and once per *worker*. Only workers run tests, so a scratch database created on
the controller is never used — it is created and immediately dropped again. The
:func:`tests.conftest._is_xdist_controller` predicate lets ``pytest_configure``
skip creation on the controller while still creating it for workers and for
serial (no ``-n``) runs. These tests pin that three-way distinction.
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


def test_xdist_controller_is_detected() -> None:
    # `-n` active (dist != "no") and no `workerinput` -> this is the controller.
    assert _is_xdist_controller(_config(dist="load", worker=False)) is True


def test_xdist_worker_is_not_a_controller() -> None:
    # `-n` active but `workerinput` present -> this is a worker, which needs its DB.
    assert _is_xdist_controller(_config(dist="load", worker=True)) is False


def test_serial_run_is_not_a_controller() -> None:
    # No `-n` (dist == "no") -> single process that needs its own scratch DB.
    assert _is_xdist_controller(_config(dist="no", worker=False)) is False


def test_missing_dist_option_is_not_a_controller() -> None:
    # Conservative default: if xdist never registered its options, treat the run
    # as serial and create the DB rather than skip it.
    assert _is_xdist_controller(_config(dist=None, worker=False)) is False
