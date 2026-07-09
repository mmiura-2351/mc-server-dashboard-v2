"""Regression guards for the session-shared app fixture (issue #1736).

The endpoint suites reuse one ``create_app`` per xdist worker via ``shared_app``;
these tests pin the two properties that make that safe: the app is the once-built
session app, and the wrapper clears ``dependency_overrides`` on both entry and
exit so one test's fakes never leak into another.
"""

from __future__ import annotations

import inspect

from fastapi import FastAPI


def _marker() -> None:  # a stand-in dependency key to register as an override
    ...


def test_shared_app_wraps_the_session_app(
    shared_app: FastAPI, _session_app: FastAPI
) -> None:
    # shared_app must reuse the once-per-worker session app, not build a new one.
    assert shared_app is _session_app
    assert isinstance(shared_app, FastAPI)


def test_shared_app_clears_overrides_on_entry_and_exit() -> None:
    # Drive the fixture generator directly over a scratch app so the clear is
    # pinned on BOTH sides within one test. A symmetric cross-test pair cannot:
    # the wrapper clears on entry, so under ``-n auto --dist worksteal`` (the api
    # suite's mode) the two tests may split across workers and the second's
    # entry-clear masks a dropped exit-clear regardless of order. A pre-seeded
    # override stands in for a prior test's leftover fakes — entry must drop it,
    # and the exit-clear must drop this test's own addition.
    from tests.conftest import shared_app

    app = FastAPI()
    app.dependency_overrides[_marker] = _marker

    # ``inspect.unwrap`` recovers the generator ``@pytest.fixture`` wrapped, so we
    # can drive its setup/teardown by hand rather than through pytest's runner.
    lifecycle = inspect.unwrap(shared_app)(app)
    entered = next(lifecycle)
    assert entered is app
    assert entered.dependency_overrides == {}  # entry cleared the leftover

    entered.dependency_overrides[_marker] = _marker
    next(lifecycle, None)  # run past the yield to finalize the fixture
    assert app.dependency_overrides == {}  # exit cleared this test's addition
