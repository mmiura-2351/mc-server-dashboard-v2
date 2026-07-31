"""Read one code out of the shared command-error contract table (issue #204).

``proto/contract/command_error_contract.json`` is the single source of truth for
which ``CommandErrorCode`` the Worker emits per (command kind, precondition), and
``tests/servers/test_command_error_contract.py`` pins the API's match sites to it.
This helper lets a *behavioural* test drive its fake control plane with the code
the Worker really answers for a precondition, instead of a hand-picked status.

That distinction is the whole point for the failed-stop-orphan refusal (issue
#2476): a test that hardcodes the status passes whatever the contract says, so it
cannot show that the API's handling of the orphan refusal is right. Reading the
row makes the test track the contract — flip the row and the behaviour under test
must still hold.
"""

from __future__ import annotations

import json
from pathlib import Path

from mc_server_dashboard_api.servers.domain.control_plane import CommandStatus

# Repo root: tests/servers/<file> -> api/ -> repo root.
_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "proto"
    / "contract"
    / "command_error_contract.json"
)


def worker_status(kind: str, precondition: str) -> CommandStatus:
    """The ``CommandStatus`` the Worker emits for ``kind`` in ``precondition``."""

    rows = json.loads(_CONTRACT_PATH.read_text())["rows"]
    for row in rows:
        if row["kind"] == kind and row["precondition"] == precondition:
            return CommandStatus(row["code"])
    raise AssertionError(
        f"the contract table has no row for ({kind}, {precondition}); "
        "add it there rather than hardcoding a status here"
    )
