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

The scan sees references only. A door that compares ``desired_state`` and
``observed_state`` itself, or that has no state gate at all, is outside it.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

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
