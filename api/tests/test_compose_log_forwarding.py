"""The bundled deployment forwards every service's log knobs (issue #2794).

``log.level`` / ``log.format`` are declared, typed and documented settings for
all three runtime processes (CONFIGURATION.md Sections 5.10 and 6.4, RELAY.md
Section 13), read at startup by ``configure_logging`` on the API and by
``newLogger`` on the Worker and the Relay. ``compose.yaml`` forwarded none of
them, so on the only way this project is deployed setting
``MCD_API_LOG__LEVEL=debug`` in ``.env`` produced no error, no warning and no
effect — the same defect class as issue #2585, on the setting an operator is
most likely to reach for during an incident.

These guards live here rather than in ``worker/`` or ``relay/`` because the
artifact under test is ``compose.yaml``, a repo-root deployment file owned by no
module; the api suite is already where its forwarding is pinned
(``test_serve_entrypoint``, ``fleet/test_control_plane_config``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mc_server_dashboard_api.config import LogSettings

_COMPOSE_FILE = Path(__file__).resolve().parents[2] / "compose.yaml"
_API_LOG_DEFAULTS = LogSettings()


def _compose_service(name: str) -> str:
    """The ``name`` service block of the shipped ``compose.yaml``.

    Scoped to one service so a same-named key under another service can never be
    picked up by a whole-file search (the reason given in issue #2596) — which
    matters more here than usual, since the three services carry keys that differ
    only in their prefix.
    """

    lines = _COMPOSE_FILE.read_text().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(f"  {name}:"))
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].strip() and not lines[i].startswith("   ")
        ),
        len(lines),
    )
    return "".join(lines[start:end])


@pytest.mark.parametrize(
    ("service", "settings"),
    [
        pytest.param(
            "api",
            (
                "MCD_API_LOG__LEVEL: "
                f'"${{MCD_API_LOG__LEVEL:-{_API_LOG_DEFAULTS.level}}}"',
                "MCD_API_LOG__FORMAT: "
                f'"${{MCD_API_LOG__FORMAT:-{_API_LOG_DEFAULTS.format}}}"',
            ),
            id="api-service",
        ),
        pytest.param(
            "worker",
            (
                'MCD_WORKER_LOG_LEVEL: "${MCD_WORKER_LOG_LEVEL:-info}"',
                'MCD_WORKER_LOG_FORMAT: "${MCD_WORKER_LOG_FORMAT:-json}"',
            ),
            id="worker-service",
        ),
        pytest.param(
            "relay",
            (
                'MCD_RELAY_LOG_LEVEL: "${MCD_RELAY_LOG_LEVEL:-info}"',
                'MCD_RELAY_LOG_FORMAT: "${MCD_RELAY_LOG_FORMAT:-json}"',
            ),
            id="relay-service",
        ),
    ],
)
def test_compose_forwards_log_settings(service: str, settings: tuple[str, str]) -> None:
    compose_service = _compose_service(service)
    for setting in settings:
        assert setting in compose_service
