"""Upload display names, sent the way an attacker can send them (issues #3922, #3957).

Every multipart upload that takes a ``display_name`` form field (the
resource-pack upload and the plugin upload) is tested against the same shapes,
sent as raw bytes through :func:`tests.servers.upload_filenames.post_raw_upload`.
"""

from __future__ import annotations

import pytest


def refused_display_names() -> list[object]:
    """``(display_name, charset)`` pairs no upload may store."""

    return [
        pytest.param(b"a\x00b", None, id="nul"),
        pytest.param(b"a\nb", None, id="lf"),
        pytest.param(b"a\x1b[2Jb", None, id="esc"),
        pytest.param(b"a\x7fb", None, id="del"),
        pytest.param("a\x85b".encode(), None, id="c1"),
        # A lone surrogate has no UTF-8 encoding, so the row could not be
        # stored; a declared charset decodes one.
        pytest.param(b"a+2AA-b", "utf-7", id="lone-surrogate"),
        pytest.param(b"   ", None, id="blank"),
        pytest.param(("x" * 201).encode(), None, id="201-characters"),
    ]


def accepted_display_names() -> list[object]:
    """``(display_name, stored)`` pairs: what an upload sends, and what it stores."""

    return [
        pytest.param("My Pack", "My Pack", id="ascii"),
        pytest.param("パック　二", "パック　二", id="japanese"),
        pytest.param(
            "\U0001f468‍\U0001f469 a‮b",
            "\U0001f468‍\U0001f469 a‮b",
            id="zwj-and-bidi",
        ),
        pytest.param("x" * 200, "x" * 200, id="200-characters"),
        pytest.param("あ" * 200, "あ" * 200, id="200-multibyte-characters"),
        # The bound is on the name as stored, after the strip.
        pytest.param("  " + "x" * 200 + "  ", "x" * 200, id="stripped"),
    ]
