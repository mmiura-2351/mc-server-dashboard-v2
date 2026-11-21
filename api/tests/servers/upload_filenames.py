"""Upload filenames, sent the way an attacker can send them (issues #3907, #3922).

Every multipart upload that keeps the part's ``filename`` (the resource-pack
upload, the plugin upload and the file upload) is tested against the same
shapes. A client library cannot deliver most unsafe names: httpx percent-encodes
every C0 character but ESC, as browsers do. :func:`post_raw_upload` sends what an
attacker can instead: raw bytes, and a declared ``charset`` the parser decodes the
parameter with, which is how a CR or a lone surrogate gets in.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx2
import pytest
from fastapi.testclient import TestClient

_BOUNDARY = b"upload-boundary"


def post_raw_upload(
    client: TestClient,
    url: str,
    filename: bytes,
    *,
    fields: Mapping[str, bytes] | None = None,
    charset: str | None = None,
    content: bytes = b"PK\x03\x04",
) -> httpx2.Response:
    """POST a multipart body whose ``file`` part's ``filename`` is these bytes.

    ``filename`` is the parameter's whole value, quotes included. ``fields`` are
    form fields sent before the file part, as raw bytes too. ``content`` is the
    file part's body.
    """

    body = b""
    for name, value in (fields or {}).items():
        body += (
            b"--" + _BOUNDARY + b"\r\n"
            b'Content-Disposition: form-data; name="'
            + name.encode()
            + b'"\r\n\r\n'
            + value
            + b"\r\n"
        )
    body += (
        b"--" + _BOUNDARY + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename=' + filename + b"\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n" + content + b"\r\n"
        b"--" + _BOUNDARY + b"--\r\n"
    )
    content_type = f"multipart/form-data; boundary={_BOUNDARY.decode()}"
    if charset is not None:
        content_type += f"; charset={charset}"
    return client.post(url, content=body, headers={"Content-Type": content_type})


def _quoted(name: str) -> bytes:
    return f'"{name}"'.encode()


def refused_filenames(ext: str) -> list[object]:
    """``(filename, charset)`` pairs no upload may keep, each ending ``ext``."""

    return [
        pytest.param(_quoted(f"a\x00b{ext}"), None, id="nul"),
        pytest.param(_quoted(f"a\nb{ext}"), None, id="lf"),
        # A raw CR ends the header (a 400); a declared charset decodes one.
        pytest.param(_quoted(f"a+AA0-b{ext}"), "utf-7", id="cr"),
        pytest.param(_quoted(f"a\tb{ext}"), None, id="tab"),
        pytest.param(_quoted(f"a\x1bb{ext}"), None, id="esc"),
        pytest.param(_quoted(f"a\x7fb{ext}"), None, id="del"),
        pytest.param(_quoted(f"a\x85b{ext}"), None, id="c1"),
        pytest.param(_quoted(f"a+2AA-b{ext}"), "utf-7", id="lone-surrogate"),
        pytest.param(_quoted(f"a/b{ext}"), None, id="slash"),
        pytest.param(_quoted(f"../../x{ext}"), None, id="traversal"),
        pytest.param(_quoted(f"a\\\\b{ext}"), None, id="backslash"),
        pytest.param(b'""', None, id="empty"),
        pytest.param(b'"   "', None, id="blank"),
        pytest.param(_quoted("a" * (256 - len(ext)) + ext), None, id="256-bytes"),
        # The limit is bytes: under 90 characters, but 256 bytes in UTF-8.
        pytest.param(
            _quoted(
                "あ" * ((256 - len(ext)) // 3) + "a" * ((256 - len(ext)) % 3) + ext
            ),
            None,
            id="256-utf8-bytes",
        ),
        # The Unicode noncharacters (issue #3922): XML 1.0 cannot carry U+FFFE or
        # U+FFFF, and the object store lists its keys in XML.
        pytest.param(_quoted(f"a\ufffeb{ext}"), None, id="u+fffe"),
        pytest.param(_quoted(f"a\uffffb{ext}"), None, id="u+ffff"),
        pytest.param(_quoted(f"a\ufdd0b{ext}"), None, id="u+fdd0"),
        pytest.param(_quoted(f"a\ufdefb{ext}"), None, id="u+fdef"),
        pytest.param(_quoted(f"a\U0001fffeb{ext}"), None, id="u+1fffe"),
        pytest.param(_quoted(f"a\U0010ffffb{ext}"), None, id="u+10ffff"),
    ]


def accepted_filenames(ext: str) -> list[object]:
    """Names an upload keeps as given, each ending ``ext``."""

    return [
        pytest.param(f"My Pack (v2) [1.21]{ext}", id="ascii"),
        pytest.param(f"パック{ext}", id="japanese"),
        pytest.param(f"リソース\u3000パック{ext}", id="ideographic-space"),
        pytest.param(f"Poke\u0301mon{ext}", id="nfd"),
        pytest.param(
            f"\U0001f468\u200d\U0001f469\u200d\U0001f467{ext}", id="zwj-emoji"
        ),
        pytest.param(f"a\u202eb{ext}", id="bidi"),
        pytest.param("a" * (255 - len(ext)) + ext, id="255-bytes"),
        pytest.param("あ" * ((255 - len(ext)) // 3) + ext, id="utf8-under-the-limit"),
        # Beside the noncharacter ranges, and an unassigned code point: of category
        # Cn, only the noncharacters are refused (issue #3922).
        pytest.param(f"a\ufdcfb\ufdf0c\ufffdd{ext}", id="beside-noncharacters"),
        pytest.param(f"a\u0378b{ext}", id="unassigned"),
    ]
