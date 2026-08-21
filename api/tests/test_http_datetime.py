"""Tests for the canonical UTC-datetime type (issue #632)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel

from mc_server_dashboard_api.http_datetime import UtcDatetime


class _Model(BaseModel):
    at: UtcDatetime
    maybe: UtcDatetime | None = None


def test_utc_datetime_serializes_with_z_suffix() -> None:
    model = _Model(at=dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc))
    assert model.model_dump(mode="json")["at"] == "2026-06-04T12:00:00Z"


def test_non_utc_offset_is_normalized_to_utc_z() -> None:
    # A +05:00 instant is the same moment as 07:00:00Z; the canonical form drops
    # the offset for the equivalent UTC ``Z`` rendering.
    plus_five = dt.timezone(dt.timedelta(hours=5))
    model = _Model(at=dt.datetime(2026, 6, 4, 12, 0, tzinfo=plus_five))
    assert model.model_dump(mode="json")["at"] == "2026-06-04T07:00:00Z"


def test_optional_field_passes_none_through() -> None:
    model = _Model(at=dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc), maybe=None)
    assert model.model_dump(mode="json")["maybe"] is None


def test_openapi_schema_pins_date_time_format() -> None:
    schema = _Model.model_json_schema()
    assert schema["properties"]["at"] == {
        "title": "At",
        "type": "string",
        "format": "date-time",
    }
    # The optional variant keeps the format on its non-null branch.
    any_of = schema["properties"]["maybe"]["anyOf"]
    assert {"type": "string", "format": "date-time"} in any_of
