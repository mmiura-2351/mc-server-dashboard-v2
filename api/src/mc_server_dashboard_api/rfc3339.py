"""RFC 3339 UTC rendering, independent of the HTTP surface (issue #2579).

:func:`serialize_utc` is the one definition of the canonical ``Z``-suffixed UTC
form. It started out inside :mod:`mc_server_dashboard_api.http_datetime`, which
wraps it into the pydantic :data:`~mc_server_dashboard_api.http_datetime.UtcDatetime`
annotation for response fields — but the same rendering also stamps the server
export manifest's ``exported_at``, which is a file format rather than an HTTP
header, and that made an application layer import an HTTP edge module.

It lives here because it is neutral: pure formatting over
:class:`datetime.datetime`, with no HTTP, config or wiring dependency. That is
why `rfc3339` is the one root-level module absent from the "no context's domain
or application imports a root-level module" contract in ``pyproject.toml`` —
the inward layers may import it, and no other root-level module.
"""

from __future__ import annotations

import datetime as dt


def serialize_utc(value: dt.datetime) -> str:
    """Render ``value`` as RFC 3339 UTC with the ``Z`` suffix."""

    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
