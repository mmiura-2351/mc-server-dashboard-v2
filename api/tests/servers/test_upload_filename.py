"""The upload filename rule (issues #3907, #3922).

The routes are probed with whole requests in their endpoint tests; this pins the
character ranges at their edges.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.domain.upload_filename import (
    is_valid_upload_filename,
)


@pytest.mark.parametrize(
    "char",
    [
        pytest.param("\ufdd0", id="u+fdd0"),
        pytest.param("\ufdef", id="u+fdef"),
        pytest.param("\ufffe", id="u+fffe"),
        pytest.param("\uffff", id="u+ffff"),
        pytest.param("\U0001fffe", id="u+1fffe"),
        pytest.param("\U0001ffff", id="u+1ffff"),
        pytest.param("\U0010fffe", id="u+10fffe"),
        pytest.param("\U0010ffff", id="u+10ffff"),
    ],
)
def test_refuses_a_noncharacter(char: str) -> None:
    # Unicode reserves these for a program's internal use; XML 1.0 cannot carry
    # U+FFFE or U+FFFF, and the object store lists its keys in XML.
    assert not is_valid_upload_filename(f"a{char}b.zip")


@pytest.mark.parametrize(
    "char",
    [
        pytest.param("\ufdcf", id="u+fdcf"),
        pytest.param("\ufdf0", id="u+fdf0"),
        pytest.param("\ufffd", id="u+fffd"),
        pytest.param("\U0001fffd", id="u+1fffd-unassigned"),
        pytest.param("\U00020000", id="u+20000"),
        pytest.param("\U0010fffd", id="u+10fffd-private-use"),
        pytest.param("\u0378", id="u+0378-unassigned"),
        pytest.param("\u200d", id="zwj"),
        pytest.param("\u202e", id="bidi"),
    ],
)
def test_accepts_a_character_beside_them(char: str) -> None:
    # Only the noncharacters are refused, not category Cn as a whole: a code
    # point unassigned today may name something tomorrow.
    assert is_valid_upload_filename(f"a{char}b.zip")
