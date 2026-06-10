"""API side of the cross-language contract guard (issue #204).

The API's convergence / special-case logic matches a command outcome on specific
``CommandStatus`` values and treats them differently from a plain dispatch
failure (e.g. an ``INVALID_STATE`` start outcome is read as "already running").
Each such match is only safe if the Worker *actually emits* that code for that
command kind. The #202 incident slipped through because the API matched
``INVALID_STATE`` for a stop-of-not-running while the Worker emits
``SERVER_NOT_FOUND`` -- both suites green because the API test hand-fed the
fabricated status to a fake control plane.

This test pins every API match site to the shared contract table
(``proto/contract/command_error_contract.json``), the same artifact the Worker's
``TestCommandErrorContract`` asserts it actually produces. A match on a
``(kind, code)`` pair the Worker never emits -- i.e. one absent from the table --
fails here. The table thus binds both sides to one source of truth: drift on the
Worker emissions fails the Worker test; an unsafe API match fails this one.

``API_MATCH_SITES`` mirrors the matches in ``lifecycle.py`` / ``files.py`` by
``file:line``. A regression guard (:func:`test_no_undeclared_match_sites`)
counts the ``CommandStatus`` references in those modules so a new match added in
source without being declared (and thus checked) here fails loudly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from mc_server_dashboard_api.servers.domain.control_plane import CommandStatus

# Repo root: tests/servers/<file> -> api/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_PATH = _REPO_ROOT / "proto" / "contract" / "command_error_contract.json"

_SRC = Path(__file__).resolve().parents[2] / "src" / "mc_server_dashboard_api"
_APP = _SRC / "servers" / "application"
_LIFECYCLE = _APP / "lifecycle.py"
_FILES = _APP / "files.py"
# command_dispatch.py maps a start failure's sanitized status onto the 409 reason
# (issue #225): another (kind, code) match site, scanned by the regression guard.
_COMMAND_DISPATCH = _APP / "command_dispatch.py"

# Worker command kinds the file use cases dispatch (the table's keys). The API's
# ``_map_file_status`` is shared across all three running-server file commands.
# FILE_ACCESS_DENIED is reachable for all three; SERVER_NOT_FOUND only for the
# read paths -- the Worker's handleEditFile creates missing intermediate dirs, so
# it never emits SERVER_NOT_FOUND (the shared helper's branch is simply dead for
# EditFile, never matched against a code the Worker produces).
_FILE_KINDS = ("ReadFile", "EditFile", "ListFiles")
_FILE_READ_KINDS = ("ReadFile", "ListFiles")

# Every (worker command kind, CommandStatus) pair the API's convergence /
# special-case logic matches on, mirroring the source. Each MUST be a code the
# Worker actually emits for that kind, i.e. present in the contract table.
API_MATCH_SITES: tuple[tuple[str, CommandStatus], ...] = (
    # lifecycle.py: redispatch_start AND __call__ (#773/#774) -- INVALID_STATE on a
    # start means the Worker already runs the server (its start guard rejected a
    # live instance), so both treat it as convergence rather than a failure.
    ("StartServer", CommandStatus.INVALID_STATE),
    # lifecycle.py: redispatch_start AND __call__ (#824) -- BUSY on a start means
    # another lifecycle command is in flight on the Worker (outcome unknown), so
    # both keep the assignment/intent and raise for a retry WITHOUT converging
    # observed=running (the distinct-from-INVALID_STATE branch).
    ("StartServer", CommandStatus.BUSY),
    # lifecycle.py: stop convergence -- SERVER_NOT_FOUND means no live instance;
    # converge observed=stopped instead of failing. The graceful-stop path also
    # special-cases the same status ("not SERVER_NOT_FOUND" raises), and
    # redispatch_stop special-cases it again to skip the final snapshot (#846).
    ("StopServer", CommandStatus.SERVER_NOT_FOUND),
    # lifecycle.py: SendServerCommand -- the server stopped between the
    # observed-running check and dispatch; the Worker emits SERVER_NOT_FOUND.
    ("ServerCommand", CommandStatus.SERVER_NOT_FOUND),
    # files.py: _map_file_status (shared by all file commands).
    *((kind, CommandStatus.SERVER_NOT_FOUND) for kind in _FILE_READ_KINDS),
    *((kind, CommandStatus.FILE_ACCESS_DENIED) for kind in _FILE_KINDS),
    # command_dispatch.py: a sanitized start failure maps its status onto the 409
    # body reason (port_conflict / image_missing) instead of command_failed (#225).
    ("StartServer", CommandStatus.PORT_CONFLICT),
    ("StartServer", CommandStatus.IMAGE_MISSING),
)


def _load_table() -> set[tuple[str, str]]:
    """Return the table as a set of (kind, code) pairs the Worker emits."""

    data = json.loads(_CONTRACT_PATH.read_text())
    return {(row["kind"], row["code"]) for row in data["rows"]}


def test_api_match_sites_are_in_contract_table() -> None:
    table = _load_table()
    for kind, status in API_MATCH_SITES:
        assert (kind, status.value) in table, (
            f"API matches {kind} outcomes on CommandStatus.{status.name}, but the "
            f"contract table has no row where the Worker emits {status.value!r} "
            f"for {kind}. Either the match is wrong (the #202 class of bug) or the "
            f"table is stale -- reconcile against the Worker's instancemanager."
        )


def test_no_undeclared_match_sites() -> None:
    """Guard against an API match added in source but not declared above.

    Counts ``CommandStatus.<NAME>`` references in the convergence/special-case
    modules. If a new match appears in source without a corresponding entry in
    ``API_MATCH_SITES``, this count diverges and fails, forcing the new pair to be
    declared (and thus checked against the table).
    """

    pattern = re.compile(r"CommandStatus\.[A-Z_]+")
    found = sum(
        len(pattern.findall(path.read_text()))
        for path in (_LIFECYCLE, _FILES, _COMMAND_DISPATCH)
    )
    # lifecycle.py has 8 CommandStatus.<NAME> references (redispatch_start and
    # __call__ both read an INVALID_STATE start as already-running (#773/#774) and
    # both special-case a BUSY start as retry-no-converge (#824), stop convergence,
    # graceful-stop "not SERVER_NOT_FOUND", redispatch_stop's snapshot-skip
    # "not SERVER_NOT_FOUND" (#846), SendServerCommand); files.py has 2
    # (_map_file_status); command_dispatch.py has 2 (the sanitized start-failure
    # reason map, issue #225). Bump this with intent when a genuinely new
    # convergence/special-case match is added -- and add it to API_MATCH_SITES so it
    # is checked against the contract table.
    assert found == 12, (
        f"found {found} CommandStatus references in lifecycle.py/files.py/"
        "command_dispatch.py, expected 12. A convergence/special-case match was "
        "added or removed: update API_MATCH_SITES (so it is checked against the "
        "contract table) and this count."
    )
