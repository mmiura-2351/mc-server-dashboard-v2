"""Every container of the bundled deployment has a bounded log (issue #4150).

Docker's ``json-file`` log driver, the daemon's default, keeps everything a
container writes to stdout and stderr in one file that grows without limit,
unless the daemon's own ``log-opts`` say otherwise. No service in
``compose.yaml`` set ``logging:``, so on a host with the stock daemon config
every service's log grew on the shared root filesystem until it had to be
truncated by hand.

The Minecraft server containers are not compose services: the Worker creates
them and bounds their logs itself (``driver.container.log_max_size`` and
``driver.container.log_max_file``, CONFIGURATION.md Section 6.3). What is pinned
here for them is that compose forwards those two keys, so an operator's ``.env``
reaches the Worker.

Static text parsing, like the other ``compose.yaml`` guards in this suite
(``test_compose_log_forwarding``): the api has no YAML dependency, and
``docker compose config`` would need the docker CLI.
"""

from __future__ import annotations

import re
from pathlib import Path

_COMPOSE_FILE = Path(__file__).resolve().parents[2] / "compose.yaml"

_BOUNDED_LOGGING_ANCHOR = (
    "x-logging: &bounded-logging\n"
    "  driver: json-file\n"
    "  options:\n"
    '    max-size: "50m"\n'
    '    max-file: "5"\n'
)
_SERVICE_LOGGING = "    logging: *bounded-logging\n"


def _service_blocks() -> dict[str, str]:
    """Each service under the top-level ``services:`` key, by name."""

    lines = _COMPOSE_FILE.read_text().splitlines(keepends=True)
    start = lines.index("services:\n") + 1
    end = next(
        (
            i
            for i in range(start, len(lines))
            if lines[i].strip() and not lines[i].startswith(" ")
        ),
        len(lines),
    )
    blocks: dict[str, list[str]] = {}
    body: list[str] = []
    for line in lines[start:end]:
        header = re.fullmatch(r"  ([\w.-]+):\n", line)
        if header:
            body = blocks.setdefault(header.group(1), [])
        else:
            body.append(line)
    return {name: "".join(body) for name, body in blocks.items()}


def test_compose_defines_a_bounded_json_file_log() -> None:
    assert _BOUNDED_LOGGING_ANCHOR in _COMPOSE_FILE.read_text()


def test_every_compose_service_bounds_its_log() -> None:
    services = _service_blocks()
    assert {"db", "api", "worker"} <= services.keys()
    unbounded = sorted(
        name for name, block in services.items() if _SERVICE_LOGGING not in block
    )
    assert unbounded == []


def test_compose_forwards_the_worker_container_log_bound() -> None:
    worker = _service_blocks()["worker"]
    for key in (
        "MCD_WORKER_DRIVER_CONTAINER_LOG_MAX_SIZE",
        "MCD_WORKER_DRIVER_CONTAINER_LOG_MAX_FILE",
    ):
        assert f'      {key}: "${{{key}:-}}"\n' in worker
