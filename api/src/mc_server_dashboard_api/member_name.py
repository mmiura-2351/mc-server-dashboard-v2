"""The member-name rule: how deep and how long a working set's names may be (#3973).

Every door that creates a name in a working set judges it by
:func:`member_name_refusal`: the files-API mutations, a rollback that creates a
deleted name again, an archive extract, a server import, a plugin's enable,
disable and side change, and the backup upload gate (issue #4008). A working set
the rule admits is one the Worker can settle, check and snapshot, and one both
backends can hold:

- at most :data:`MAX_DIRECTORY_DEPTH` directory levels, counted as the Worker
  counts them: the working-set root is level 0 and only directories count, so a
  directory may have 128 components and a file 129;
- at most :data:`MAX_COMPONENT_BYTES` bytes in a name's one component, Linux's
  ``NAME_MAX``;
- at most :data:`MAX_PATH_BYTES` bytes in the whole path, the Worker's snapshot
  member bound. It is an upper bound only: issue #3979 decides the final number.

The numbers are the cross-language contract in
``proto/contract/member_name_limits.json``. Which characters a name may carry is
not part of the rule: a running server can create any name, and the platform's
own backups and exports of it must upload again. The files-API doors refuse the
characters an uploaded file's name may not carry on top of it.

It is a neutral root-level module that imports only the standard library, so
both the servers and the storage contexts' inward layers may import it. That is
the ``rfc3339`` precedent in the root-level import contract.
"""

from __future__ import annotations

MAX_DIRECTORY_DEPTH = 128
MAX_COMPONENT_BYTES = 255
MAX_PATH_BYTES = 4095

# The reasons, named as the 422 problem reasons the files-API doors answer with.
NAME_TOO_LONG = "name_too_long"
PATH_TOO_LONG = "path_too_long"
PATH_TOO_DEEP = "path_too_deep"


def member_name_refusal(path: str, *, is_dir: bool) -> str | None:
    """Why no working set may hold ``path``, or ``None`` when one may.

    ``path`` is slash-separated and relative to the working-set root, and
    ``is_dir`` says whether it names a directory, whose last component is a
    level, or a file, whose last component is not. Empty and ``.`` segments are
    dropped first, as a ``RelPath`` drops them: they name the same place. A
    ``..`` is not this rule's to judge.

    Lengths are bytes as the Worker counts them, so a name is measured in UTF-8,
    and a surrogate escape, as ``tarfile`` decodes a byte that is not UTF-8,
    counts as the one byte it stands for. Any other lone surrogate has no bytes
    to count: ``path`` must not carry one.
    """

    sizes = _component_sizes(path)
    if any(size > MAX_COMPONENT_BYTES for size in sizes):
        return NAME_TOO_LONG
    components, size = _path_size(sizes)
    return extent_refusal(
        directories=components if is_dir else components - 1, path_bytes=size
    )


def component_refusal(path: str) -> str | None:
    """``name_too_long`` when a component of ``path`` is past 255 bytes, else ``None``.

    The part of :func:`member_name_refusal` that does not depend on where the
    name sits, with its precondition: what a rename judges its destination by
    whatever the move grows, since its components are new.
    """

    if any(size > MAX_COMPONENT_BYTES for size in _component_sizes(path)):
        return NAME_TOO_LONG
    return None


def extent_refusal(*, directories: int, path_bytes: int) -> str | None:
    """Why no working set may hold a name this deep and this long, or ``None``.

    ``directories`` is the directory level the name reaches, and ``path_bytes``
    its length. A directory move judges by it the deepest and the longest names
    it creates below its destination, without naming each one.
    """

    if path_bytes > MAX_PATH_BYTES:
        return PATH_TOO_LONG
    if directories > MAX_DIRECTORY_DEPTH:
        return PATH_TOO_DEEP
    return None


def path_size(path: str) -> tuple[int, int]:
    """``path``'s components and bytes, measured as :func:`member_name_refusal`
    measures them, with its precondition."""

    return _path_size(_component_sizes(path))


def _component_sizes(path: str) -> list[int]:
    return [
        len(part.encode("utf-8", "surrogateescape"))
        for part in path.split("/")
        if part not in ("", ".")
    ]


def _path_size(sizes: list[int]) -> tuple[int, int]:
    return len(sizes), sum(sizes) + max(len(sizes) - 1, 0)
