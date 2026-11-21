"""The member-name rule: depth, component and path length (issue #3973).

Depth is counted as the Worker counts it: the working-set root is level 0, and
only directories count. So a directory may have 128 components and a file 129.
The numbers are the cross-language contract in
``proto/contract/member_name_limits.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mc_server_dashboard_api.member_name import (
    MAX_COMPONENT_BYTES,
    MAX_DIRECTORY_DEPTH,
    MAX_PATH_BYTES,
    NAME_TOO_LONG,
    PATH_TOO_DEEP,
    PATH_TOO_LONG,
    member_name_refusal,
)

_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "proto"
    / "contract"
    / "member_name_limits.json"
)


def _nested(directories: int, leaf: str | None = None) -> str:
    """A path of ``directories`` one-letter directories, then ``leaf`` if given."""

    parts = ["d"] * directories
    if leaf is not None:
        parts.append(leaf)
    return "/".join(parts)


def test_the_limits_are_the_contract_s() -> None:
    contract = json.loads(_CONTRACT.read_text())

    assert (MAX_DIRECTORY_DEPTH, MAX_COMPONENT_BYTES, MAX_PATH_BYTES) == (
        contract["max_directory_depth"],
        contract["max_component_bytes"],
        contract["max_path_bytes"],
    )


def test_a_directory_128_levels_deep_is_admitted() -> None:
    assert member_name_refusal(_nested(128), is_dir=True) is None


def test_a_directory_129_levels_deep_is_too_deep() -> None:
    assert member_name_refusal(_nested(129), is_dir=True) == PATH_TOO_DEEP


def test_a_file_in_a_directory_128_levels_deep_is_admitted() -> None:
    # 129 components: the file itself is not a level.
    assert member_name_refusal(_nested(128, "f"), is_dir=False) is None


def test_a_file_in_a_directory_129_levels_deep_is_too_deep() -> None:
    assert member_name_refusal(_nested(129, "f"), is_dir=False) == PATH_TOO_DEEP


@pytest.mark.parametrize(
    "path",
    [
        # Empty and ``.`` segments are not levels: they name the same place.
        "/".join(["d", ""] * 128 + ["f"]),
        "/".join(["d", "."] * 128 + ["f"]),
        _nested(128) + "/",
    ],
)
def test_empty_and_dot_segments_are_not_levels(path: str) -> None:
    assert member_name_refusal(path, is_dir=False) is None


def test_a_component_of_255_bytes_is_admitted() -> None:
    assert member_name_refusal("world/" + "n" * 255, is_dir=False) is None


def test_a_component_of_256_bytes_is_too_long() -> None:
    assert member_name_refusal("world/" + "n" * 256, is_dir=False) == NAME_TOO_LONG


def test_a_directory_component_of_256_bytes_is_too_long() -> None:
    assert member_name_refusal("n" * 256 + "/f", is_dir=False) == NAME_TOO_LONG


def test_a_component_is_measured_in_utf8_bytes() -> None:
    # 85 three-byte characters are 255 bytes; 86 are 258.
    assert member_name_refusal("\u3042" * 85, is_dir=False) is None
    assert member_name_refusal("\u3042" * 86, is_dir=False) == NAME_TOO_LONG


def test_a_byte_tarfile_cannot_decode_counts_as_one_byte() -> None:
    # ``tarfile`` decodes a name byte that is not UTF-8 as a surrogate escape. The
    # Worker counts the name's bytes, so the escape counts as the one byte it is.
    assert member_name_refusal("\udcff" * 255, is_dir=False) is None
    assert member_name_refusal("\udcff" * 256, is_dir=False) == NAME_TOO_LONG


def _path_of(size: int) -> str:
    """A path of ``size`` bytes in components of 200 bytes, then the rest."""

    parts: list[str] = []
    left = size
    while left > 201:
        parts.append("p" * 200)
        left -= 201
    parts.append("p" * left)
    path = "/".join(parts)
    assert len(path.encode()) == size
    return path


def test_a_path_of_4095_bytes_is_admitted() -> None:
    assert member_name_refusal(_path_of(4095), is_dir=False) is None


def test_a_path_of_4096_bytes_is_too_long() -> None:
    assert member_name_refusal(_path_of(4096), is_dir=False) == PATH_TOO_LONG


def test_a_path_is_measured_without_empty_and_dot_segments() -> None:
    # ``./`` and ``//`` name the same place, so they add no bytes.
    assert (
        member_name_refusal("./" + _path_of(4095).replace("/", "//"), is_dir=False)
        is None
    )


def test_the_root_is_admitted() -> None:
    assert member_name_refusal("", is_dir=True) is None
    assert member_name_refusal(".", is_dir=True) is None
