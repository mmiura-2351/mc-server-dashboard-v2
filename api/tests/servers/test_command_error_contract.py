"""API side of the cross-language contract guard (issue #204).

The API's convergence / special-case logic matches a command outcome on specific
``CommandStatus`` values and treats them differently from a plain dispatch
failure (e.g. an ``INVALID_STATE`` start outcome is read as "already running").
Each such match is only safe if the Worker *actually emits* that code for that
command kind. The #202 incident slipped through because the API matched
``INVALID_STATE`` for a stop-of-not-running while the Worker emits
``SERVER_NOT_FOUND`` -- both suites green because the API test hand-fed the
fabricated status to a fake control plane.

Since issue #2472 the shared table
(``proto/contract/command_error_contract.json``) carries the API's handling on
every row, and this module DERIVES its expectations from that column instead of
maintaining its own list and count:

* the table names, per row, the sites that match the row's ``(kind, code)`` --
  ``module.py:qualname`` inside ``servers/application/`` -- or says the outcome is
  absorbed by the catch-all (``command_failed``) or never read at all
  (``fire_and_forget``);
* :func:`test_declared_api_sites_match_the_source` asserts the
  ``(module, qualname, CommandStatus)`` triples the table declares are EXACTLY the
  triples an ``ast`` scan finds in the application layer.

That makes both directions consequences of the table rather than thresholds
somebody can nudge: a new match on a ``(kind, code)`` the Worker never emits has
no row to be declared on (the #202 class), a declared site deleted from the source
fails here, and a match added in source without a row fails here too. The Worker's
``TestCommandErrorContract`` and ``TestContractTableIsExhaustive`` hold the other
direction -- that every row is what the instancemanager really emits, and that
every (kind, precondition) cell HAS a row.

Granularity note: a site is ``(module, enclosing qualname, status)``. Two
references to the same status inside one function are one site; the qualname is
class-qualified, so ``StartServer.__call__`` and ``StopServer.__call__`` are
distinct. Only ``servers/application/`` is scanned: ``servers/adapters/
control_plane.py`` also names every ``CommandStatus``, but that is the wire
translation building the outcome, not a match on one.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from mc_server_dashboard_api.servers.domain.control_plane import CommandStatus

# Repo root: tests/servers/<file> -> api/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_PATH = _REPO_ROOT / "proto" / "contract" / "command_error_contract.json"

_SRC = Path(__file__).resolve().parents[2] / "src" / "mc_server_dashboard_api"
_APPLICATION = _SRC / "servers" / "application"

# Codes that carry no API handling: a success, and the marker for a cell whose
# precondition determines no emission for that kind (issue #2472).
_NO_HANDLING_CODES = frozenset({"ok", "unaffected"})

# The two non-site handlings a row may declare instead of a list.
_CATCH_ALL = "command_failed"
_FIRE_AND_FORGET = "fire_and_forget"

# Prefix marking an api entry that CONSUMES a discriminator declared on the same
# row rather than matching the status itself (the periodic snapshot scheduler
# reading ``is_working_set_absent_refusal``, issue #2480). It holds no
# ``CommandStatus`` reference, so the source scan cannot see it; it is recorded so
# the row names every place the refusal is acted on, and skipped by the check.
_VIA = "via "

Triple = tuple[str, str, str]
Row = dict[str, Any]


def _rows() -> list[Row]:
    data: Any = json.loads(_CONTRACT_PATH.read_text())
    rows: list[Row] = data["rows"]
    return rows


def _declared_triples() -> dict[Triple, set[str]]:
    """The (module, qualname, status name) triples the table's api column names.

    Maps each triple to the command kinds whose rows declare it, so a failure can
    say which contract row a stale site belongs to.
    """

    declared: dict[Triple, set[str]] = {}
    for row in _rows():
        api = row.get("api")
        if not isinstance(api, list):
            continue
        status = CommandStatus(row["code"]).name
        for entry in api:
            if entry.startswith(_VIA):
                continue
            module, _, qualname = entry.partition(":")
            declared.setdefault((module, qualname, status), set()).add(row["kind"])
    return declared


def _modules() -> list[tuple[str, ast.Module]]:
    """Every application-layer module, keyed by its path relative to the layer.

    ``rglob`` rather than ``glob``: a use case moved into a subpackage would
    otherwise drop out of the scan silently, and a match site nobody scans is a
    match site nobody checks — the drift this module exists to end. A nested module
    keys as ``sub/mod.py``, so the key stays unique and an api entry naming it says
    where it is.
    """

    return [
        (path.relative_to(_APPLICATION).as_posix(), ast.parse(path.read_text()))
        for path in sorted(_APPLICATION.rglob("*.py"))
    ]


def _status_names(node: ast.AST) -> list[str]:
    """Every ``CommandStatus.<NAME>`` referenced anywhere under ``node``."""

    return [
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "CommandStatus"
    ]


def _assigned_name(stmt: ast.stmt) -> str | None:
    """The target name of a module-level assignment, for attributing its refs."""

    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                return target.id
    return None


def _collect(body: list[ast.stmt], owner: str, module: str, found: set[Triple]) -> None:
    for stmt in body:
        if isinstance(stmt, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            qualname = f"{owner}.{stmt.name}" if owner else stmt.name
            _collect(stmt.body, qualname, module, found)
            continue
        name = owner or _assigned_name(stmt) or "<module>"
        for status in _status_names(stmt):
            found.add((module, name, status))


def _scanned_triples() -> set[Triple]:
    """Every ``CommandStatus`` match site in the servers application layer."""

    found: set[Triple] = set()
    for module, tree in _modules():
        _collect(tree.body, "", module, found)
    return found


def _collect_definitions(
    body: list[ast.stmt], owner: str, module: str, defined: set[tuple[str, str]]
) -> None:
    for stmt in body:
        if isinstance(stmt, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            qualname = f"{owner}.{stmt.name}" if owner else stmt.name
            defined.add((module, qualname))
            _collect_definitions(stmt.body, qualname, module, defined)
        elif not owner:
            name = _assigned_name(stmt)
            if name is not None:
                defined.add((module, name))


def _defined_qualnames() -> set[tuple[str, str]]:
    """Every ``(module, qualname)`` the application layer defines.

    Classes, functions and methods under the same class-qualified naming the match
    scan uses, plus module-level assignments — the full set of things an api entry
    can name.
    """

    defined: set[tuple[str, str]] = set()
    for module, tree in _modules():
        _collect_definitions(tree.body, "", module, defined)
    return defined


def test_declared_api_sites_match_the_source() -> None:
    """The table's api column and the application layer name the same sites.

    Both directions matter. A ``CommandStatus`` match in source that no row
    declares is the #202 class of bug -- the API acting on a code for a kind the
    Worker may never emit -- and it has nowhere to be declared unless the Worker
    really emits it, because the api column lives ON the row that pins the
    emission. A declared site the source no longer has is stale self-description,
    the drift issue #2472 set out to end.
    """

    declared = _declared_triples()
    scanned = _scanned_triples()

    undeclared = sorted(scanned - set(declared))
    assert not undeclared, (
        "these CommandStatus match sites are in servers/application/ but no "
        f"contract row declares them: {undeclared}. Add the site to the 'api' "
        "column of the row for the (kind, code) it matches in "
        "proto/contract/command_error_contract.json -- and if there is no such "
        "row, the API is matching a code the Worker never emits for that kind "
        "(the #202 incident)."
    )

    stale = sorted(triple for triple in declared if triple not in scanned)
    assert not stale, (
        "the contract table declares these API match sites, but the source has "
        f"no such CommandStatus reference: {stale}. They were renamed, moved or "
        "removed -- update the 'api' column of the rows that name them (kinds: "
        f"{ {t: sorted(declared[t]) for t in stale} })."
    )


def test_declared_via_sites_exist_in_the_source() -> None:
    """A ``via`` entry names a real place too, or it rots unnoticed.

    A ``via`` entry is a consumer of a discriminator declared on the same row — it
    holds no ``CommandStatus`` reference of its own, so
    :func:`test_declared_api_sites_match_the_source` cannot see it and, until this
    test, nothing checked it at all: the entry could name a function that was
    renamed or deleted and the suite stayed green, which is exactly the stale
    self-description issue #2472 set out to end. Existence is all this can check
    (whether that function really consumes the discriminator is not visible to an
    ``ast`` scan), and it is what catches the rot.
    """

    defined = _defined_qualnames()
    missing = []
    for row in _rows():
        api = row.get("api")
        if not isinstance(api, list):
            continue
        for entry in api:
            if not entry.startswith(_VIA):
                continue
            module, _, qualname = entry[len(_VIA) :].partition(":")
            if (module, qualname) not in defined:
                missing.append((f"({row['kind']}, {row['precondition']})", entry))

    assert not missing, (
        "these contract rows declare a 'via' consumer the application layer does "
        f"not define: {missing}. It was renamed, moved or removed -- update the "
        "'api' column in proto/contract/command_error_contract.json."
    )


def test_every_error_row_declares_its_api_handling() -> None:
    """Each row with a real error code says how the API treats it (issue #2472)."""

    for row in _rows():
        where = f"({row['kind']}, {row['precondition']})"
        api = row.get("api")
        if row["code"] in _NO_HANDLING_CODES:
            assert api is None, (
                f"row {where} has code {row['code']!r}, which the API never handles; "
                "drop its 'api' entry."
            )
            continue
        assert api is not None, (
            f"row {where} emits {row['code']!r} but does not say how the API treats "
            "it. Add 'api': a list of the sites that match it, "
            f"{_CATCH_ALL!r} when it falls through to the generic dispatch failure, "
            f"or {_FIRE_AND_FORGET!r} when the API awaits no result for the kind."
        )
        if isinstance(api, str):
            assert api in (_CATCH_ALL, _FIRE_AND_FORGET), (
                f"row {where} declares an unknown api handling {api!r}"
            )
            continue
        assert api, f"row {where} declares an empty api site list"
        for entry in api:
            site = entry[len(_VIA) :] if entry.startswith(_VIA) else entry
            module, sep, qualname = site.partition(":")
            assert sep and qualname and module.endswith(".py"), (
                f"row {where} declares the api site {entry!r}; the form is "
                "'module.py:qualname' (optionally prefixed 'via ')"
            )


def test_rows_sharing_a_kind_and_code_declare_the_same_api() -> None:
    """One (kind, code) pair is handled one way, however many rows produce it.

    Two preconditions can answer the same code -- ``{StartServer, orphan_pending}``
    and ``{StartServer, command_in_flight}`` both answer BUSY -- and the API cannot
    tell them apart: it sees the code. So their rows must not disagree about what
    happens to it, or one of them is describing the API wrongly.
    """

    by_pair: dict[tuple[str, str], list[Row]] = {}
    for row in _rows():
        if row["code"] in _NO_HANDLING_CODES:
            continue
        by_pair.setdefault((row["kind"], row["code"]), []).append(row)

    for (kind, code), rows in by_pair.items():
        handlings = {json.dumps(row.get("api"), sort_keys=True) for row in rows}
        assert len(handlings) == 1, (
            f"rows for ({kind}, {code}) disagree about the API handling: "
            f"{sorted(handlings)}. The API matches on the code, not on the "
            "precondition, so every row producing this pair describes the same "
            "handling."
        )


def test_invalid_state_has_a_single_meaning_on_the_start_and_hydrate_paths() -> None:
    """Pin INVALID_STATE to one meaning on the reserve()-gated paths (issue #2496).

    ``StartServer.__call__`` and ``redispatch_start`` read an ``INVALID_STATE``
    outcome on a start (or a hydrate) as "the instance is already running" and
    CONVERGE ``observed=running`` off it, keeping the assignment and the running
    intent (#213/#773/#774). ``redispatch_start``'s arm is deliberately NOT gated
    on whether the start leg was actually reached, so ``_launch`` returning the
    outcome of a REFUSED HYDRATE lands in that same arm. That is sound only while
    ``INVALID_STATE`` carries exactly ONE meaning on both reserve()-gated kinds:
    ``instance_running`` (an instance is demonstrably live). A refused hydrate then
    still proves the instance is up, whichever leg produced the code.

    Nothing but this pin and a reader's vigilance enforces that uniqueness. If a
    second precondition were later routed to ``invalid_state`` on ``StartServer``
    or ``HydrateTrigger`` -- the mistake ``reserve()`` originally made by answering
    it for a pending failed-stop orphan as well, which #2467 spent four PRs
    unwinding and #2476 fixed by moving that case to ``busy`` -- the convergence arm
    would silently manufacture a false ``observed=running`` from a leg that never
    started a process, reopening the #2467 wedge. Assert the uniqueness against the
    contract table so adding a second meaning turns THIS red first, instead of the
    wedge reappearing in production. The already-running row itself is load-bearing
    (the convergence exists for it), so its removal reddens this too.
    """

    rows = _rows()
    for kind in ("StartServer", "HydrateTrigger"):
        invalid_state_preconditions = {
            row["precondition"]
            for row in rows
            if row["kind"] == kind and row["code"] == "invalid_state"
        }
        assert invalid_state_preconditions == {"instance_running"}, (
            f"the API converges observed=running off an INVALID_STATE {kind} outcome "
            f"(issue #2496), which stays sound only while 'already running' "
            f"(instance_running) is its ONLY meaning on this reserve()-gated kind. "
            f"The contract table now has {kind} answering invalid_state for "
            f"{sorted(invalid_state_preconditions)}. A second meaning here lets a leg "
            f"that never started a process manufacture a false observed=running (the "
            f"#2467 wedge): route the new precondition to BUSY as the failed-stop "
            f"orphan was (#2476), or gate the convergence arm on start-leg evidence "
            f"(issue #2496 option 1) -- do not let it converge. If instance_running "
            f"itself is missing, the load-bearing convergence lost its backing row."
        )
