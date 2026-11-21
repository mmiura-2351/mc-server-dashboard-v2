"""Unit tests for the community value objects (pure, no I/O)."""

import pytest

from mc_server_dashboard_api.community.domain.errors import (
    InvalidCommunityNameError,
    InvalidPermissionError,
    InvalidRoleNameError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityName,
    Permission,
    RoleName,
)


def test_community_name_must_not_be_blank() -> None:
    with pytest.raises(InvalidCommunityNameError):
        CommunityName("   ")


def test_community_name_trims_surrounding_whitespace() -> None:
    assert CommunityName("  guild  ").value == "guild"


def test_role_name_must_not_be_blank() -> None:
    with pytest.raises(InvalidRoleNameError):
        RoleName("  ")


def test_role_name_trims_surrounding_whitespace() -> None:
    assert RoleName("  Owner  ").value == "Owner"


# A community's and a role's name are shown in lists and chips, as a server's is,
# so they follow its rule (issue #3988): no control character (Unicode ``Cc``),
# no lone surrogate, which has no UTF-8 encoding and so could not be stored, and
# no line or paragraph separator (``Zl``, ``Zp``).
_REFUSED_NAMES = [
    pytest.param("a\x00b", id="nul"),
    pytest.param("a\nb", id="lf"),
    pytest.param("a\tb", id="tab"),
    pytest.param("a\x1b[2Jb", id="esc"),
    pytest.param("a\x7fb", id="del"),
    pytest.param("a\x85b", id="c1"),
    pytest.param("a\ud800b", id="lone-surrogate"),
    pytest.param("a\u2028b", id="u+2028"),
    pytest.param("a\u2029b", id="u+2029"),
]

_ACCEPTED_NAMES = [
    pytest.param("ギルド\u3000二", id="japanese"),
    pytest.param("\U0001f468\u200d\U0001f469 a\u202eb", id="zwj-and-bidi"),
]


@pytest.mark.parametrize("name", _REFUSED_NAMES)
def test_community_name_refuses_a_character_no_name_may_carry(name: str) -> None:
    with pytest.raises(InvalidCommunityNameError) as excinfo:
        CommunityName(name)
    # The message names the rule, not the refused name.
    assert name not in str(excinfo.value)


@pytest.mark.parametrize("name", _REFUSED_NAMES)
def test_role_name_refuses_a_character_no_name_may_carry(name: str) -> None:
    with pytest.raises(InvalidRoleNameError) as excinfo:
        RoleName(name)
    assert name not in str(excinfo.value)


@pytest.mark.parametrize("name", _ACCEPTED_NAMES)
def test_community_name_accepts_any_other_character(name: str) -> None:
    assert CommunityName(name).value == name


@pytest.mark.parametrize("name", _ACCEPTED_NAMES)
def test_role_name_accepts_any_other_character(name: str) -> None:
    assert RoleName(name).value == name


def test_names_trim_a_surrounding_line_break_rather_than_refusing_it() -> None:
    assert CommunityName("\tguild\r\n").value == "guild"
    assert RoleName("\u2028Builder\u2029").value == "Builder"


def test_permission_accepts_resource_action_shape() -> None:
    assert Permission("server:start").value == "server:start"


@pytest.mark.parametrize(
    "code", ["server", "server:", ":start", "serverstart", "  server:start  "]
)
def test_permission_rejects_malformed_codes(code: str) -> None:
    with pytest.raises(InvalidPermissionError):
        Permission(code)


def test_permission_rejects_extra_colon() -> None:
    with pytest.raises(InvalidPermissionError):
        Permission("server:start:now")
