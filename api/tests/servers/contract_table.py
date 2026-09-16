"""Read one code or message out of the shared command-error contract table (#204).

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

:func:`worker_message` is the same idea for the refusals the API discriminates by
TEXT (issue #2843). ``is_working_set_absent_refusal`` keys on a phrase inside the
Worker's message, and the fixtures feeding it were hand copies of a Go literal that
nothing checked: a reword on the Worker side dropped the match with both suites
green. The message is declared in the shared table now, the Worker test asserts its
emission against it, and a fixture reads it from here instead of restating it.
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


def worker_message(name: str) -> str:
    """The refusal text the Worker emits, as the table's ``messages`` declares it.

    A ``%s`` in it stands for a runtime value (the working dir), left for the
    caller to fill in; the Worker test pins the text around it.
    """

    messages = json.loads(_CONTRACT_PATH.read_text())["messages"]
    for message in messages:
        if message["name"] == name:
            text: str = message["text"]
            return text
    raise AssertionError(
        f"the contract table declares no message named {name!r}; declare it there, "
        "on the rows whose cells emit it, rather than hand-copying the Worker literal"
    )
