"""Regression guards for the session-shared app fixture (issue #1736).

The endpoint suites reuse one ``create_app`` per xdist worker via ``shared_app``;
these tests pin the two properties that make that safe: the app is the once-built
session app, and ``dependency_overrides`` is cleared between tests so one test's
fakes never leak into another.
"""

from __future__ import annotations

from fastapi import FastAPI


def _marker() -> None:  # a stand-in dependency key to register as an override
    ...


def test_shared_app_wraps_the_session_app(
    shared_app: FastAPI, _session_app: FastAPI
) -> None:
    # shared_app must reuse the once-per-worker session app, not build a new one.
    assert shared_app is _session_app
    assert isinstance(shared_app, FastAPI)


def test_shared_app_overrides_isolated_a(shared_app: FastAPI) -> None:
    # If the wrapper failed to clear, whichever of these two tests ran second on a
    # given worker would observe the other's override and fail.
    assert shared_app.dependency_overrides == {}
    shared_app.dependency_overrides[_marker] = _marker


def test_shared_app_overrides_isolated_b(shared_app: FastAPI) -> None:
    assert shared_app.dependency_overrides == {}
    shared_app.dependency_overrides[_marker] = _marker
