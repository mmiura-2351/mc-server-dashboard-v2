"""Tests for the canonical UTC-datetime type (issue #632)."""

from __future__ import annotations

from pydantic import BaseModel

from mc_server_dashboard_api.http_datetime import UtcDatetime


class _Model(BaseModel):
    at: UtcDatetime
    maybe: UtcDatetime | None = None


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
