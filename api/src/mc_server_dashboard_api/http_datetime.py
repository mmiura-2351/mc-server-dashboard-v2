"""Canonical UTC-datetime serialization for the HTTP surface (issue #632).

The HTTP API exposed the same logical UTC instant two ways depending on how the
field was modelled:

* a real :class:`datetime.datetime` response field let pydantic-core render it,
  which emits the RFC 3339 ``Z`` suffix for UTC (e.g. ``GET /api/audit``,
  ``GET /api/workers``); while
* a ``str`` field populated with ``value.isoformat()`` emitted the ``+00:00``
  offset for the *same* UTC instant (e.g. ``GET /api/admin/users``,
  ``GET /api/users/me/sessions``, ``GET /api/backups/statistics``).

Both are valid ISO-8601, but the split tripped strict clients and was invisible
from the OpenAPI spec because neither path pinned a ``format``.

:data:`UtcDatetime` is the one canonical type for every UTC-datetime field on
the HTTP surface. It:

* normalizes the value to UTC and serializes with the ``Z`` suffix (the form
  the audit/workers endpoints already used and the more strict-client-friendly
  convention); and
* pins ``format: date-time`` in the OpenAPI schema so the contract is explicit.

Model UTC-datetime response fields as :data:`UtcDatetime` (or
``UtcDatetime | None``) rather than as a ``str`` filled by ``.isoformat()``, so
the serialization stays consistent as new models are added.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import PlainSerializer, WithJsonSchema


def _serialize_utc(value: dt.datetime) -> str:
    """Render ``value`` as RFC 3339 UTC with the ``Z`` suffix."""

    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[
    dt.datetime,
    PlainSerializer(_serialize_utc, return_type=str),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
"""A UTC ``datetime`` field that serializes to RFC 3339 with the ``Z`` suffix."""
