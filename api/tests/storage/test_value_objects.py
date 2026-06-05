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


@pytest.mark.parametrize(
    "raw",
    [
        "world/\x00null.dat",  # NUL
        "config/foo\tbar.txt",  # TAB (a C0 control)
        "config/foo\x1f.txt",  # unit separator (top of the C0 range)
        "config/foo\x7f.txt",  # DEL
        "a\rb/level.dat",  # CR
        "a\nb/level.dat",  # LF
        "config\r\n/foo.txt",  # CRLF (header-injection shape)
    ],
)
def test_relpath_rejects_control_characters(raw: str) -> None:
    with pytest.raises(PathTraversalError):
        RelPath(raw)


@pytest.mark.parametrize(
    "raw",
    [
        'config/say "hi".txt',  # double quote stays allowed (header helper quotes)
        "world/世界/レベル.dat",  # non-ASCII unicode filenames are legitimate
        "config/naïve café.json",  # accented latin-1
    ],
)
def test_relpath_accepts_quotes_and_unicode(raw: str) -> None:
    # Quotes and unicode are legal filename content; only ASCII control chars are
    # rejected. The Content-Disposition helper neutralises quotes at reflection.
    assert RelPath(raw).value == raw


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
