"""Benchmark: domain value-object validation (issue #1122).

Measures the construction cost of the community-context value objects that
carry validation logic (CommunityName, Permission).  These are on the hot
path of every authorised request.

To add a new validation benchmark, add a ``test_bench_<model>`` function
that calls ``benchmark(constructor, *args)``.
"""

from __future__ import annotations

from typing import Any

from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityName,
    Permission,
)


def test_bench_community_name(benchmark: Any) -> None:
    """Construction + whitespace-trim of CommunityName."""
    benchmark(CommunityName, "  My Community  ")


def test_bench_permission(benchmark: Any) -> None:
    """Construction + shape validation of Permission."""
    benchmark(Permission, "server:create")
