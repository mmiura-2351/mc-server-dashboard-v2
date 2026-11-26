"""The bundled object store keeps 5% of its filesystem free (issue #4149).

``weed server`` marks every volume read-only once free space on its filesystem
drops below ``-volume.minFreeSpace``. Unset, SeaweedFS falls back to 1%. On a
single host the store shares that filesystem with PostgreSQL, the Worker's
scratch and Docker, so the store could take it down to the last 1%, and
PostgreSQL then stopped on a full disk (issue #4085). DEPLOYMENT.md Section 5
documents the headroom this reserve is part of.

Static text parsing, like the other ``compose.yaml`` guards in this suite: the
api has no YAML dependency. Only the ``exec weed server`` line is read, as the
argv the shell passes, so a comment that names the flag cannot satisfy the test.
"""

from __future__ import annotations

import shlex

from tests.test_compose_log_forwarding import _compose_service


def _weed_server_argv() -> list[str]:
    """The argv the seaweedfs entrypoint execs ``weed server`` with."""

    (command,) = (
        line.strip()
        for line in _compose_service("seaweedfs").splitlines()
        if line.strip().startswith("exec weed server ")
    )
    return shlex.split(command)


def test_seaweedfs_keeps_five_percent_of_its_filesystem_free() -> None:
    minimums = [
        arg for arg in _weed_server_argv() if arg.startswith("-volume.minFreeSpace=")
    ]
    assert minimums == ["-volume.minFreeSpace=5"]
