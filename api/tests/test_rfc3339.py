"""Tests for the canonical RFC 3339 UTC rendering (issues #632, #2579)."""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.rfc3339 import serialize_utc


def test_serialize_utc_renders_z_suffix_for_utc() -> None:
    # The shared helper backing raw-dict timestamps (SSE ``ts``, export
    # ``exported_at``; #674) emits the canonical ``Z`` form, not ``+00:00``.
    value = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
    assert serialize_utc(value) == "2026-06-04T12:00:00Z"


def test_serialize_utc_normalizes_offset_to_utc_z() -> None:
    plus_five = dt.timezone(dt.timedelta(hours=5))
    value = dt.datetime(2026, 6, 4, 12, 0, tzinfo=plus_five)
    assert serialize_utc(value) == "2026-06-04T07:00:00Z"
