"""Value-object validation for the storage domain (STORAGE.md Sections 3.2, 6).

``RelPath`` carries the string-level traversal defence; ``JarKey`` enforces the
content-address shape.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.storage.domain.errors import PathTraversalError
from mc_server_dashboard_api.storage.domain.value_objects import JarKey, RelPath


def test_relpath_normalises_dot_and_redundant_separators() -> None:
    assert RelPath("./world//level.dat").value == "world/level.dat"
    assert RelPath("server.properties").parts == ("server.properties",)


@pytest.mark.parametrize("raw", ["", "."])
def test_relpath_root_has_empty_parts(raw: str) -> None:
    # "" / "." denote the server root itself (legal for list_dir of the root).
    assert RelPath(raw).parts == ()
    assert RelPath(raw).value == "."


@pytest.mark.parametrize("raw", ["/etc/passwd", "/world/level.dat"])
def test_relpath_rejects_absolute(raw: str) -> None:
    with pytest.raises(PathTraversalError):
        RelPath(raw)


@pytest.mark.parametrize("raw", ["../escape", "world/../../escape", "..", "a/../../b"])
def test_relpath_rejects_parent_traversal(raw: str) -> None:
    with pytest.raises(PathTraversalError):
        RelPath(raw)


def test_jarkey_accepts_valid_sha256() -> None:
    digest = "a" * 64
    assert JarKey(digest).sha256 == digest


@pytest.mark.parametrize(
    "bad",
    ["", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65],
)
def test_jarkey_rejects_non_sha256(bad: str) -> None:
    with pytest.raises(ValueError):
        JarKey(bad)
