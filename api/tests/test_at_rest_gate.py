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

The write-site scan is syntactic too, and these pass it:

- a helper or gate helper named like another in its module: names resolve
  module-wide, so ``B._check`` is taken for a refusing ``A._check``, and an
  injected collaborator's ``self.sync(...)`` for a module function ``sync``;
- a ``_private`` write helper that another module imports: it is judged only at
  its callers in its own module;
- a new caller of an allowed public writer such as
  ``regenerate_pending_group_files``: its entry names the callers it has, and
  nothing checks another;
- a gate that only co-locates: one referenced after the write, or only logged,
  still counts. Sibling closures in one function share its verdict, and so does
  the code under an ``if`` or a ``try`` in a module or class body, which is
  judged with that body;
- a gate helper's refusal, which is matched by its shape: which server
  ``<name>`` holds is not checked, and a raise of ``not_quiescent_error(...)``
  counts under any condition;
- ``rollback`` named but not called, on a receiver not named ``*store`` or
  ``*storage``, as in ``functools.partial(files.rollback, ...)``.
"""

from __future__ import annotations

import abc
import ast
from collections import Counter
from collections.abc import Iterator
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
# Every reference in ``src`` to a method that changes a server's working set
# sits in a function that calls ``is_quiescent()`` or ``not_quiescent_error()``,
# or is on the allowlist below. A write is any attribute named for one of the
# ports' write methods, on any receiver and whether called or not (an alias, a
# ``functools.partial``, ``asyncio.to_thread``), and the name as a string
# (``getattr``). Only ``rollback``, which a unit of work and a session share,
# counts only when it is called with arguments or on a receiver named ``*store``
# or ``*storage``. The code in a module or class body outside its functions is a
# door of its own, named ``<module>`` or after the class.
#
# Two kinds of function are judged through others. A private helper (``_name``)
# that its module references is judged at whatever references it, by a call, a
# callback, a ``functools.partial`` or its name as a string: its writes count as
# theirs, so an ungated door that reaches a gated door's helper fails. And a
# private function that refuses gates the callers that call it directly. It
# refuses when it raises ``<name>.not_quiescent_error(...)``, or raises right
# under ``if not <name>.is_quiescent():`` (``_load_quiescent_server``), and the
# raise is in neither a nested function nor a ``try`` that catches. One that
# only reads, logs or returns the predicate (``reconciler._is_settled``) does
# not. A port adapter's own write methods are its callers' to gate. Names are
# matched within one module, so a public function another module calls is judged
# as a door. The module docstring lists what the scan still does not see.

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


# A write method named like a unit of work's or a session's: it counts only when
# it is called with arguments, as the port's is and theirs never is, or on a
# receiver named ``*store`` or ``*storage``.
_COLLIDING = frozenset({"rollback"})


def _receiver_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _asks(node: ast.expr | None, method: str) -> bool:
    """Whether ``node`` is ``<name>.<method>(...)``, asked of a plain name."""

    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
        and isinstance(node.func.value, ast.Name)
    )


def _propagating(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Each statement in or under ``body`` whose ``raise`` would leave it.

    A nested ``def`` or ``class`` is not entered, nor the body of a ``try`` that
    has a handler.
    """

    for stmt in body:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        catches = isinstance(stmt, (ast.Try, ast.TryStar)) and bool(stmt.handlers)
        blocks = [getattr(stmt, "orelse", []), getattr(stmt, "finalbody", [])]
        if not catches:
            blocks.append(getattr(stmt, "body", []))
        blocks += [
            part.body
            for part in (*getattr(stmt, "handlers", ()), *getattr(stmt, "cases", ()))
        ]
        for block in blocks:
            yield from _propagating(block)


def _refuses(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether ``node`` raises the gate's error, or raises unless quiescent."""

    for stmt in _propagating(node.body):
        if isinstance(stmt, ast.Raise) and _asks(stmt.exc, "not_quiescent_error"):
            return True
        if (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.UnaryOp)
            and isinstance(stmt.test.op, ast.Not)
            and _asks(stmt.test.operand, "is_quiescent")
            and any(isinstance(sub, ast.Raise) for sub in stmt.body)
        ):
            return True
    return False


class _Unit:
    """A function or method, or a module or class body outside its functions."""

    def __init__(
        self,
        name: str,
        nodes: list[ast.AST],
        *,
        implements_port: bool = False,
        refuses: bool = False,
    ) -> None:
        self.name = name
        self.implements_port = implements_port
        self.refuses = refuses
        self.writes: Counter[str] = Counter()
        # Names called directly, f(...) or self.f(...), and every name the unit
        # references, by attribute, by name or as a string.
        self.calls: set[str] = set()
        self.refs: set[str] = set()
        self.gated = False
        subs = [sub for node in nodes for sub in ast.walk(node)]
        with_args = {
            id(sub.func)
            for sub in subs
            if isinstance(sub, ast.Call) and (sub.args or sub.keywords)
        }
        for sub in subs:
            if isinstance(sub, ast.Name):
                self.refs.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                self.refs.add(sub.attr)
                if sub.attr in _GATES:
                    self.gated = True
                if sub.attr in _WRITES and (
                    sub.attr not in _COLLIDING
                    or id(sub) in with_args
                    or _receiver_name(sub.value).endswith(("store", "storage"))
                ):
                    self.writes[sub.attr] += 1
            elif isinstance(sub, ast.Constant):
                if isinstance(sub.value, str):
                    self.refs.add(sub.value)
                    if sub.value in _WRITES:
                        self.writes[sub.value] += 1
            elif isinstance(sub, ast.Call):
                func = sub.func
                if isinstance(func, ast.Name):
                    self.calls.add(func.id)
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in ("self", "cls")
                ):
                    self.calls.add(func.attr)


def _units(source: str) -> dict[str, _Unit]:
    units: dict[str, _Unit] = {}

    def visit(body: list[ast.stmt], qualname: str, bases: frozenset[str]) -> None:
        # A module or class body: each function and class in it is a unit of its
        # own, and the rest, with what sits under an ``if`` or a ``try``, is one.
        prefix = "" if qualname == "<module>" else f"{qualname}."
        rest: list[ast.AST] = []
        for node in body:
            if isinstance(node, ast.ClassDef):
                rest += [*node.decorator_list, *node.bases, *node.keywords]
                names = frozenset(
                    base.id if isinstance(base, ast.Name) else base.attr
                    for base in node.bases
                    if isinstance(base, (ast.Name, ast.Attribute))
                )
                visit(node.body, prefix + node.name, names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                units[prefix + node.name] = _Unit(
                    node.name,
                    [node],
                    implements_port=bool(bases & _PORTS) and node.name in _WRITES,
                    refuses=_refuses(node),
                )
            else:
                rest.append(node)
        if rest:
            units[qualname] = _Unit("<body>", rest)

    visit(ast.parse(source).body, "<module>", frozenset())
    return units


def _is_helper(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _ungated_writes(source: str) -> dict[str, Counter[str]]:
    """Each door in ``source`` that reaches a working-set write with no gate."""

    units = _units(source)
    gate_helpers = {
        unit.name for unit in units.values() if _is_helper(unit.name) and unit.refuses
    }

    def gated(unit: _Unit) -> bool:
        return unit.gated or bool(unit.calls & gate_helpers)

    def called(unit: _Unit) -> bool:
        return any(
            unit.name in other.refs for other in units.values() if other is not unit
        )

    def reached(unit: _Unit, seen: frozenset[str]) -> Counter[str]:
        writes = Counter(unit.writes)
        for qualname, helper in units.items():
            if (
                helper.name in unit.refs
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
import functools


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


def _require_quiescent(server):
    if not server.is_quiescent():
        raise RuntimeError


class GatedByRaise:
    async def __call__(self, server):
        _require_quiescent(server)
        await self.file_store.make_dir(rel_path="d")


async def _load_released(uow, server_id):
    async with uow:
        server = await uow.get(server_id)
        if server.assigned_worker_id is not None:
            raise server.not_quiescent_error(RuntimeError)
    return server


class GatedByErrorRaise:
    async def __call__(self, server_id):
        await _load_released(self.uow, server_id)
        await self.file_store.make_dir(rel_path="d")


def _is_settled(server):
    return server.is_quiescent()


async def reseed(server, file_store):
    _is_settled(server)
    await file_store.write_file(rel_path="a")


class Ungated:
    async def __call__(self, server):
        await self.uow.rollback()
        await self.file_store.rollback(rel_path="a")
        await self.backup_store.restore(server)
        await self._publish(server)

    async def _publish(self, server):
        await self.file_store.write_file(rel_path="a")


class Adapter(FileStore):
    async def write_file(self, rel_path):
        await self._storage.write_file(rel_path)


class OtherReceiver:
    async def __call__(self, server):
        await self.gated._rewrite(server)


class Callback:
    async def __call__(self, server_id):
        await self.locked(server_id, populate=self.gated._rewrite)


class ByName:
    async def __call__(self, server):
        await getattr(self.gated, "_rewrite")(server)


async def _seed(file_store):
    await file_store.write_file(rel_path="s")


class GatedSeed:
    async def __call__(self, server):
        if not server.is_quiescent():
            raise server.not_quiescent_error(RuntimeError)
        await _seed(self.file_store)


async def by_partial(file_store):
    await functools.partial(_seed, file_store)()


async def _populate(file_store):
    await file_store.write_file(rel_path="p")


class GatedCallback:
    async def __call__(self, server):
        if not server.is_quiescent():
            raise server.not_quiescent_error(RuntimeError)
        await self.locked(server.id, populate=_populate)


class GatedByReference:
    async def __call__(self, server_id):
        await self.run(_load_quiescent, self.uow, server_id)
        await self.file_store.make_dir(rel_path="d")


def require_quiescent(server):
    if not server.is_quiescent():
        raise server.not_quiescent_error(RuntimeError)


class GatedByPublic:
    async def __call__(self, server):
        require_quiescent(server)
        await self.file_store.make_dir(rel_path="d")
"""
    # A helper that only reads the predicate refuses nothing, so it gates no
    # caller (reconciler._is_settled is one). A helper reached by reference is
    # judged at whoever reaches it, but gates only a direct call, and only a
    # private function gates at all.
    assert _ungated_writes(source) == {
        "reseed": Counter({"write_file": 1}),
        "Ungated.__call__": Counter({"rollback": 1, "restore": 1, "write_file": 1}),
        "OtherReceiver.__call__": Counter({"write_file": 1}),
        "Callback.__call__": Counter({"write_file": 1}),
        "ByName.__call__": Counter({"write_file": 1}),
        "by_partial": Counter({"write_file": 1}),
        "GatedByReference.__call__": Counter({"make_dir": 1}),
        "GatedByPublic.__call__": Counter({"make_dir": 1}),
    }


@pytest.mark.parametrize(
    "helper",
    [
        pytest.param(
            """
def _check(server):
    if not server.is_quiescent():
        _LOG.warning("%s", server.not_quiescent_error(RuntimeError))
""",
            id="logs-the-error",
        ),
        pytest.param(
            """
def _check(server):
    return server.not_quiescent_error(RuntimeError)
""",
            id="returns-the-error",
        ),
        pytest.param(
            """
def _check(server):
    try:
        if not server.is_quiescent():
            raise server.not_quiescent_error(RuntimeError)
    except RuntimeError:
        pass
""",
            id="swallows-the-raise",
        ),
        pytest.param(
            """
def _check(server):
    if not server.is_quiescent():
        def fail():
            raise server.not_quiescent_error(RuntimeError)
        return fail
""",
            id="raises-in-a-nested-def",
        ),
        pytest.param(
            """
def _check(server, strict=False):
    if not server.is_quiescent():
        if strict:
            raise RuntimeError
""",
            id="raises-under-a-further-condition",
        ),
        pytest.param(
            """
def _check(server):
    if not server.peer().is_quiescent():
        raise RuntimeError
""",
            id="asks-another-object",
        ),
    ],
)
def test_a_helper_that_does_not_refuse_gates_no_caller(helper: str) -> None:
    source = (
        helper
        + """

async def door(server, file_store):
    _check(server)
    await file_store.make_dir(rel_path="d")
"""
    )
    assert _ungated_writes(source) == {"door": Counter({"make_dir": 1})}


def test_the_write_scan_sees_a_write_however_it_is_spelled() -> None:
    source = """
import asyncio
import functools


class LocalAlias:
    async def __call__(self):
        files = self.file_store
        await files.write_file(rel_path="a")


async def parameter(files, rel_path):
    await files.delete_dir(rel_path=rel_path)


class MethodAlias:
    async def __call__(self):
        write = self.file_store.write_file
        await write(rel_path="a")


class OtherNames:
    async def __call__(self, server):
        await self.files.write_file(rel_path="a")
        await self._backend.restore_backup(server)


class Computed:
    async def __call__(self, server):
        await self._store_for(server).write_file(rel_path="a")
        await self.stores[0].make_dir(rel_path="d")


class Dynamic:
    async def __call__(self):
        await getattr(self.file_store, "write_file")(rel_path="a")
        await functools.partial(self.file_store.rename_file, from_path="a")()
        await asyncio.to_thread(self.storage.rollback_file, "a")


class RollbackAlias:
    async def __call__(self, rel_path, version_id):
        files = self.file_store
        await files.rollback(rel_path=rel_path, version_id=version_id)
        await self.uow.rollback()
"""
    assert _ungated_writes(source) == {
        "LocalAlias.__call__": Counter({"write_file": 1}),
        "parameter": Counter({"delete_dir": 1}),
        "MethodAlias.__call__": Counter({"write_file": 1}),
        "OtherNames.__call__": Counter({"write_file": 1, "restore_backup": 1}),
        "Computed.__call__": Counter({"write_file": 1, "make_dir": 1}),
        "Dynamic.__call__": Counter(
            {"write_file": 1, "rename_file": 1, "rollback_file": 1}
        ),
        "RollbackAlias.__call__": Counter({"rollback": 1}),
    }


def test_the_write_scan_sees_a_write_outside_a_function() -> None:
    source = """
_put = FileStore.write_file
_drop = lambda files: files.delete_file(rel_path="a")


class Holder:
    rename = FileStore.rename_file

    if True:
        async def mkdir(self):
            await self.file_store.make_dir(rel_path="d")


if True:
    async def wipe(files):
        await files.delete_dir(rel_path="w")


@hooks.after(FileStore.make_dir)
class Hooked:
    pass


class Door:
    async def __call__(self, files):
        await _put(files, rel_path="a")
"""
    # Each module or class body is a door of its own: the code outside its
    # functions, what sits under an ``if`` or a ``try`` there, and the
    # decorators and bases of the classes in it.
    assert _ungated_writes(source) == {
        "<module>": Counter(
            {"write_file": 1, "delete_file": 1, "delete_dir": 1, "make_dir": 1}
        ),
        "Holder": Counter({"rename_file": 1, "make_dir": 1}),
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
