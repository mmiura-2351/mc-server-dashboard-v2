"""Flags the bundled object store's ``weed server`` line must carry.

The store keeps 5% of its filesystem free (issue #4149). ``weed server`` marks
every volume read-only once free space on its filesystem drops below
``-volume.minFreeSpace``. Unset, SeaweedFS falls back to 1%. On a single host
the store shares that filesystem with PostgreSQL, the Worker's scratch and
Docker, so the store could take it down to the last 1%, and PostgreSQL then
stopped on a full disk (issue #4085). DEPLOYMENT.md Section 5 documents the
headroom this reserve is part of.

The store does not bind the Lance Namespace listener (issue #4207). Unset,
``-s3.port.lance`` defaults to 9101, and SeaweedFS 4.54 then serves it on the
container's address to every peer on the ``mcsd`` network. Nothing in this
deployment uses it. SECURITY.md Section 6 lists it as disabled by flag.

The store logs to stderr only, and runs as the image's ``seaweed`` user
(issue #4206). The compose entrypoint replaces the image's own, which passes
``-logtostderr=true`` and drops root with ``su-exec seaweed``. Without the
flag, weed's glog also writes its whole log as files in the container's
``/tmp``, outside the json-file bound. Without the ``su-exec``, weed runs as
root. The ``chown`` before it re-owns a volume the root-run weed wrote, so an
existing deployment keeps working.

The store sets its own volume budget (issue #2786). Unset, ``-volume.max``
defaults to 8. The bucket and the filer's metadata log are two collections,
and each asks for 7 volumes at once, so the first to ask took 7 and the other
got 1: the live store sat at 8/8 with no slot left to grow. DEPLOYMENT.md
Section 5, "Disk headroom", gives the reason for the number.

Static text parsing, like the other ``compose.yaml`` guards in this suite: the
api has no YAML dependency. Only the entrypoint's ``exec`` line is read, as the
argv the shell passes, with a ``#`` comment dropped as the shell drops it. A
comment that names the flag, on its own line or after the command, therefore
cannot satisfy the test.
"""

from __future__ import annotations

import shlex

from tests.test_compose_log_forwarding import _compose_service


def _weed_server_argv() -> list[str]:
    """The argv of the ``exec`` that ends the seaweedfs entrypoint and starts weed."""

    (command,) = (
        line.strip()
        for line in _compose_service("seaweedfs").splitlines()
        if line.strip().startswith("exec ")
    )
    return shlex.split(command, comments=True)


def test_seaweedfs_keeps_five_percent_of_its_filesystem_free() -> None:
    minimums = [
        arg for arg in _weed_server_argv() if arg.startswith("-volume.minFreeSpace=")
    ]
    assert minimums == ["-volume.minFreeSpace=5"]


def test_seaweedfs_does_not_bind_the_lance_namespace_listener() -> None:
    lance_ports = [
        arg for arg in _weed_server_argv() if arg.startswith("-s3.port.lance=")
    ]
    assert lance_ports == ["-s3.port.lance=0"]


def test_seaweedfs_sets_its_volume_budget() -> None:
    budgets = [arg for arg in _weed_server_argv() if arg.startswith("-volume.max=")]
    assert budgets == ["-volume.max=70"]


def test_seaweedfs_logs_to_stderr_only() -> None:
    argv = _weed_server_argv()
    # A glog option of ``weed`` itself: ``weed server`` does not accept it.
    weed_options = argv[argv.index("weed") + 1 : argv.index("server")]
    assert "-logtostderr=true" in weed_options


def test_seaweedfs_runs_weed_as_the_seaweed_user() -> None:
    argv = _weed_server_argv()
    assert argv[: argv.index("weed")] == ["exec", "su-exec", "seaweed"]
    commands = {
        line.strip()
        for line in _compose_service("seaweedfs").splitlines()
        if not line.strip().startswith("#")
    }
    assert "chown -R seaweed:seaweed /data" in commands
