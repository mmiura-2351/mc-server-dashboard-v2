"""No API-side write to a server's working set gates on ``is_at_rest()`` alone.

A stop keeps its Worker assigned until the final snapshot settles, and that
snapshot publishes over the store (the #1703 exemption lets the assigned Worker
through). So a server at rest is not yet safe to write: an at-rest write made
while the row is still assigned is overwritten (issue #3998, after #1704 and
#3272 fixed the same invariant door by door). Every at-rest write door gates on
``Server.is_quiescent()``, at rest AND unassigned, instead.

This scan keeps the next door from regressing: any reference to ``is_at_rest``
in ``src`` outside the allowlist below fails, and so does a use the allowlist
no longer matches. Each allowed use is keyed by its module and enclosing
function, with the number of references it makes and why ``is_at_rest`` alone
is right there. A new write door must use ``is_quiescent()``; a new read that
truly may serve the store while a Worker still holds the row is added here with
its reason.

The scan sees references, by attribute or by name as a string (``getattr``,
``operator.methodcaller``). A door that compares ``desired_state`` and
``observed_state`` itself, or that has no state gate at all, is outside it, and
so is a write added to a read it allows. The write-site scan below covers those
(issue #4047): it starts from the writes rather than from the predicate.
"""

from __future__ import annotations

import abc
import ast
from collections import Counter
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.storage.domain.port import Storage

_SRC = Path(__file__).resolve().parent.parent / "src" / "mc_server_dashboard_api"

# (module, enclosing qualname) -> (references, reason).
_ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("servers/domain/entities.py", "Server.is_quiescent"): (
        1,
        "the predicate itself: at rest and unassigned",
    ),
    ("servers/domain/entities.py", "Server.not_quiescent_error"): (
        1,
        "names why a door refused: still assigned, or not at rest",
    ),
    ("servers/application/files.py", "ReadFile.__call__"): (
        1,
        "a read: serving the store while the snapshot is in flight writes nothing",
    ),
    ("servers/application/files.py", "ListDir.__call__"): (1, "a read, as ReadFile"),
    ("servers/application/files.py", "DownloadFile._require_at_rest"): (
        1,
        "a read, as ReadFile",
    ),
    ("servers/application/files.py", "SearchFiles._require_at_rest"): (
        1,
        "a read, as ReadFile",
    ),
    ("servers/application/manage_server.py", "DeleteServer.__call__"): (
        2,
        "refuses an assigned row right after, with its own ServerAssignedError",
    ),
    ("servers/application/release_held_server.py", "ReleaseHeldServer.__call__"): (
        1,
        "a release, not a write to the working set: it requires the Worker "
        "still assigned, and drops only the assignment",
    ),
    ("servers/application/schedule_runner.py", "_precondition_skip"): (
        2,
        "a pre-check only: StartServer and CreateBackup refuse an assigned row "
        "themselves, and the runner defers the backup",
    ),
}


def _uses(source: str) -> Counter[str]:
    """Each enclosing qualname in ``source`` that references ``is_at_rest``."""

    found: Counter[str] = Counter()
    scope: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def _scoped(
            self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ) -> None:
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        visit_ClassDef = _scoped
        visit_FunctionDef = _scoped
        visit_AsyncFunctionDef = _scoped

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if node.attr == "is_at_rest":
                found[".".join(scope) or "<module>"] += 1
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            # The name as a string: getattr, operator.methodcaller, attrgetter.
            if node.value == "is_at_rest":
                found[".".join(scope) or "<module>"] += 1

    _Visitor().visit(ast.parse(source))
    return found


def test_the_scan_counts_references_and_not_prose() -> None:
    source = '''
class Door:
    """Gates on ``server.is_at_rest()``."""

    async def __call__(self, server):
        # is_at_rest() in a comment is not a reference.
        if not server.is_at_rest():
            raise RuntimeError
'''
    assert _uses(source) == Counter({"Door.__call__": 1})


def test_the_scan_counts_a_reference_by_name() -> None:
    source = """
import operator

async def door(server):
    if not getattr(server, "is_at_rest")():
        raise RuntimeError
    if not operator.methodcaller("is_at_rest")(server):
        raise RuntimeError
"""
    assert _uses(source) == Counter({"door": 2})


def test_is_at_rest_is_referenced_only_where_it_is_allowed() -> None:
    found = {
        (path.relative_to(_SRC).as_posix(), qualname): count
        for path in sorted(_SRC.rglob("*.py"))
        for qualname, count in _uses(path.read_text(encoding="utf-8")).items()
    }
    unexpected = {
        f"{module}: {qualname} ({count})"
        for (module, qualname), count in found.items()
        if _ALLOWED.get((module, qualname), (0, ""))[0] != count
    }
    stale = {
        f"{module}: {qualname}"
        for (module, qualname) in _ALLOWED
        if (module, qualname) not in found
    }
    assert not unexpected, (
        "A server at rest may still be assigned to a Worker whose final snapshot "
        "will publish over the store (issue #3998). Gate an at-rest write on "
        "Server.is_quiescent() and refuse with server.not_quiescent_error(...); "
        "add a read to _ALLOWED with its reason only if it writes nothing:\n"
        + "\n".join(sorted(unexpected))
    )
    assert not stale, "Allowed uses no longer found:\n" + "\n".join(sorted(stale))


# --- Write sites (issue #4047) ----------------------------------------------
#
# Every call in ``src`` to a method that changes a server's working set sits in
# a function that calls ``is_quiescent()`` or ``not_quiescent_error()``, or is on
# the allowlist below. A call counts as a write when it calls one of the ports'
# write methods on a receiver named ``*store`` or ``*storage`` (so a unit of
# work's ``rollback`` is not one).
#
# Two kinds of function are judged through others. A private helper (``_name``)
# that its module calls is judged at its callers: its writes count as theirs, so
# an ungated door that calls a gated door's helper fails. And a same-module
# function that calls the gate itself, such as ``_load_quiescent_server``,
# gates its callers. A port adapter's own write methods are its callers' to
# gate. Calls are matched by bare name (``f(...)``, ``self.f(...)``) within one
# module, so a public function another module calls is judged as a door.

# Each port's abstract methods: those that change a working set, and the rest.
# A method a port gains fails test_every_port_method_is_sorted until it is
# placed on one side.
_PORT_METHODS: dict[abc.ABCMeta, tuple[frozenset[str], frozenset[str]]] = {
    FileStore: (
        frozenset(
            {
                "write_file",
                "delete_file",
                "delete_dir",
                "rename_file",
                "rename_dir",
                "make_dir",
                "rollback",
            }
        ),
        # Reads, and a retain into the version ring, which leaves current alone.
        frozenset(
            {
                "validate_rel_path",
                "read_file",
                "open_file_stream",
                "list_dir",
                "path_exists",
                "subtree_extent",
                "download_dir",
                "export_dir",
                "list_versions",
                "read_version",
                "retain_if_changed",
            }
        ),
    ),
    BackupArchiveStore: (
        frozenset({"restore", "prune_to_final_snapshot"}),
        # Reads of current, and writes to the archives only.
        frozenset(
            {
                "create_from_current",
                "list_archive_refs",
                "check_backup_health",
                "check_current_health",
                "delete",
                "open",
                "store",
                "size",
            }
        ),
    ),
    Storage: (
        frozenset(
            {
                "write_file",
                "delete_file",
                "delete_dir",
                "rename_file",
                "rename_dir",
                "make_dir",
                "rollback_file",
                "restore_backup",
                "prune_to_final_snapshot",
                "commit_snapshot",
            }
        ),
        # Reads; staging, which commit_snapshot publishes; the version ring; JARs
        # and archives.
        frozenset(
            {
                "open_hydrate_source",
                "begin_snapshot",
                "write_snapshot",
                "abort_snapshot",
                "current_generation",
                "settled_generation",
                "current_publisher",
                "check_current_health",
                "put_jar",
                "has_jar",
                "open_jar",
                "jar_pool_stats",
                "list_jars",
                "delete_jar",
                "create_backup_from_current",
                "list_backups",
                "check_backup_health",
                "delete_backup",
                "open_backup",
                "put_backup",
                "backup_size",
                "read_file",
                "open_file_stream",
                "list_dir",
                "path_exists",
                "subtree_extent",
                "open_working_set_view",
                "list_file_versions",
                "retain_file_version",
                "read_file_version",
            }
        ),
    ),
}
_WRITES = frozenset().union(*(writes for writes, _ in _PORT_METHODS.values()))
_PORTS = frozenset(port.__name__ for port in _PORT_METHODS)
_GATES = frozenset({"is_quiescent", "not_quiescent_error"})

# (module, door qualname) -> (writes, reason): a door that writes with no gate,
# each write method it reaches with how often, and why no gate is right there.
_WRITES_ALLOWED: dict[tuple[str, str], tuple[dict[str, int], str]] = {
    ("dataplane/api/transfers.py", "publish_snapshot"): (
        {"commit_snapshot": 1},
        "the Worker's publish, which the hold waits for; the data plane's "
        "generation and publisher guards fence it",
    ),
    ("servers/application/manage_server.py", "CreateServer.__call__"): (
        {"write_file": 1},
        "the create-time seed, under the lifecycle lock taken before the row "
        "commits: no Worker has held the server yet (#4045)",
    ),
    ("servers/application/export_import.py", "ImportServer.__call__"): (
        {"write_file": 1},
        "publishes the archive in CreateServer's populate callback, inside the "
        "same lock hold (#4045)",
    ),
    ("servers/application/lifecycle.py", "StartServer.__call__"): (
        {"write_file": 1},
        "refuses a running intent and an assigned row itself, under the "
        "lifecycle lock, before it writes eula.txt",
    ),
    ("servers/application/groups.py", "regenerate_pending_group_files"): (
        {"write_file": 1},
        "a start's owed group files: StartServer and place_and_start call it "
        "under the lifecycle lock on a row they read unassigned",
    ),
    ("servers/application/manage_server.py", "DeleteServer.__call__"): (
        {"prune_to_final_snapshot": 1},
        "refuses an assigned row itself, with ServerAssignedError, before the prune",
    ),
}


class _Unit:
    """One module-level function or method, as the write-site scan sees it."""

    def __init__(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, bases: frozenset[str]
    ) -> None:
        self.name = node.name
        self.implements_port = bool(bases & _PORTS) and node.name in _WRITES
        self.writes: Counter[str] = Counter()
        self.calls: set[str] = set()
        self.gated = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr in _GATES:
                self.gated = True
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if isinstance(func, ast.Name):
                self.calls.add(func.id)
            elif isinstance(func, ast.Attribute):
                receiver = func.value
                if isinstance(receiver, ast.Name) and receiver.id in ("self", "cls"):
                    self.calls.add(func.attr)
                receiver_name = (
                    receiver.attr
                    if isinstance(receiver, ast.Attribute)
                    else getattr(receiver, "id", "")
                )
                if func.attr in _WRITES and receiver_name.endswith(
                    ("store", "storage")
                ):
                    self.writes[func.attr] += 1


def _units(source: str) -> dict[str, _Unit]:
    units: dict[str, _Unit] = {}

    def visit(body: list[ast.stmt], prefix: str, bases: frozenset[str]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                names = frozenset(
                    base.id if isinstance(base, ast.Name) else base.attr
                    for base in node.bases
                    if isinstance(base, (ast.Name, ast.Attribute))
                )
                visit(node.body, f"{prefix}{node.name}.", names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                units[prefix + node.name] = _Unit(node, bases)

    visit(ast.parse(source).body, "", frozenset())
    return units


def _is_helper(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _ungated_writes(source: str) -> dict[str, Counter[str]]:
    """Each door in ``source`` that reaches a working-set write with no gate."""

    units = _units(source)
    gate_helpers = {unit.name for unit in units.values() if unit.gated}

    def gated(unit: _Unit) -> bool:
        return unit.gated or bool(unit.calls & gate_helpers)

    def called(unit: _Unit) -> bool:
        return any(
            unit.name in other.calls for other in units.values() if other is not unit
        )

    def reached(unit: _Unit, seen: frozenset[str]) -> Counter[str]:
        writes = Counter(unit.writes)
        for qualname, helper in units.items():
            if (
                helper.name in unit.calls
                and _is_helper(helper.name)
                and qualname not in seen
                and not gated(helper)
                and not helper.implements_port
            ):
                writes += reached(helper, seen | {qualname})
        return writes

    doors: dict[str, Counter[str]] = {}
    for qualname, unit in units.items():
        if unit.implements_port or gated(unit):
            continue
        if _is_helper(unit.name) and called(unit):
            continue  # judged at its callers
        writes = reached(unit, frozenset({qualname}))
        if writes:
            doors[qualname] = writes
    return doors


@pytest.mark.parametrize("port", list(_PORT_METHODS), ids=lambda port: port.__name__)
def test_every_port_method_is_sorted(port: abc.ABCMeta) -> None:
    writes, others = _PORT_METHODS[port]
    assert not writes & others
    assert writes | others == port.__abstractmethods__


def test_the_write_scan_judges_helpers_by_their_callers() -> None:
    source = """
async def _load_quiescent(uow, server_id):
    server = await uow.get(server_id)
    if not server.is_quiescent():
        raise server.not_quiescent_error(RuntimeError)
    return server


class Gated:
    async def __call__(self, server):
        if not server.is_quiescent():
            raise server.not_quiescent_error(RuntimeError)
        await self._rewrite(server)

    async def _rewrite(self, server):
        await self.file_store.write_file(rel_path="a")


class GatedByHelper:
    async def __call__(self, server_id):
        await _load_quiescent(self.uow, server_id)
        await self.file_store.make_dir(rel_path="d")


class Ungated:
    async def __call__(self, server):
        await self.uow.rollback()
        await self.backup_store.restore(server)
        await self._publish(server)

    async def _publish(self, server):
        await self.file_store.write_file(rel_path="a")


class Adapter(FileStore):
    async def write_file(self, rel_path):
        await self._storage.write_file(rel_path)
"""
    assert _ungated_writes(source) == {
        "Ungated.__call__": Counter({"restore": 1, "write_file": 1})
    }


def test_every_working_set_write_is_gated_or_allowed() -> None:
    found = {
        (path.relative_to(_SRC).as_posix(), qualname): writes
        for path in sorted(_SRC.rglob("*.py"))
        for qualname, writes in _ungated_writes(
            path.read_text(encoding="utf-8")
        ).items()
    }
    unexpected = {
        f"{module}: {qualname} {dict(sorted(writes.items()))}"
        for (module, qualname), writes in found.items()
        if _WRITES_ALLOWED.get((module, qualname), ({}, ""))[0] != writes
    }
    stale = {
        f"{module}: {qualname}"
        for (module, qualname) in _WRITES_ALLOWED
        if (module, qualname) not in found
    }
    assert not unexpected, (
        "These write a server's working set without asking Server.is_quiescent() "
        "(issue #4047). A final snapshot still in flight would publish over the "
        "write. Gate the door with `if not server.is_quiescent(): raise "
        "server.not_quiescent_error(...)`, or add it to _WRITES_ALLOWED with "
        "its writes and the reason no gate is right there:\n"
        + "\n".join(sorted(unexpected))
    )
    assert not stale, "Allowed writes no longer found:\n" + "\n".join(sorted(stale))
