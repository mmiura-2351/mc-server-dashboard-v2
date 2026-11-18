"""gzip streams of a tar, for the verdicts of the gzip layer (issues #3835, #3847).

Every gzip read site reads a ``tar.gz`` through ``archive_member.GzipReader``:
both upload doors, and restore and the health check on both storage backends.
So each site is tested against the same shapes, built here from the site's own
tar. Standard library only, so ``tests/test_archive_member.py`` can use it
outside the project environment too.
"""

from __future__ import annotations

import gzip
import itertools
import struct
import zlib

# How many gzip members a stream may hold.
MAX_MEMBERS = 10_000

# A gzip member that holds nothing: 20 bytes, header and trailer.
EMPTY_MEMBER = gzip.compress(b"", mtime=0)

_FHCRC = 0x02


def gzipped(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


def with_a_header_crc(data: bytes, *, matches: bool) -> bytes:
    """``data`` as one gzip member whose header carries a CRC (FHCRC).

    The CRC is the low 16 bits of the CRC32 of the header before it; with
    ``matches=False`` one bit of it is flipped.
    """

    header = b"\x1f\x8b\x08" + bytes([_FHCRC]) + bytes(4) + b"\x00\xff"
    crc = zlib.crc32(header) & 0xFFFF
    return header + struct.pack("<H", crc if matches else crc ^ 1) + gzipped(data)[10:]


def with_a_reserved_flag(data: bytes, flag: int = 0x20) -> bytes:
    """``data`` as one gzip member with a reserved FLG bit set: 0x20, 0x40 or 0x80."""

    member = bytearray(gzipped(data))
    member[3] |= flag
    return bytes(member)


def in_members(data: bytes, cuts: list[int]) -> list[bytes]:
    """``data`` cut at ``cuts``, each piece its own gzip member."""

    bounds = [0, *cuts, len(data)]
    return [gzipped(data[start:end]) for start, end in itertools.pairwise(bounds)]


def refused(tar: bytes) -> dict[str, bytes]:
    """``tar`` gzipped as every gzip read site refuses it.

    More members than the cap, a non-zero byte after the stream or between two
    members, with or without zero padding before it, and a member header with a
    CRC that does not match or a reserved flag bit (#3847).
    """

    member = gzipped(tar)
    first, second = in_members(tar, [len(tar) // 2])
    return {
        "over-the-member-cap": member + EMPTY_MEMBER * MAX_MEMBERS,
        "a-non-zero-byte-after-the-stream": member + b"\x01",
        "zero-padding-then-a-non-zero-byte": member + bytes(1024) + b"\x01",
        "a-non-zero-byte-between-members": first + b"\x01" + second,
        "a-header-crc-that-does-not-match": with_a_header_crc(tar, matches=False),
        "a-reserved-flag-bit": with_a_reserved_flag(tar),
    }


def accepted(tar: bytes) -> dict[str, bytes]:
    """``tar`` gzipped as every gzip read site accepts it.

    As many members as the cap allows, zero padding between and after members,
    and a member header with a CRC that matches.
    """

    first, second = in_members(tar, [len(tar) // 2])
    return {
        "at-the-member-cap": gzipped(tar) + EMPTY_MEMBER * (MAX_MEMBERS - 1),
        "zero-padding-between-and-after-members": (
            first + bytes(512) + second + bytes(1024)
        ),
        "a-header-crc-that-matches": with_a_header_crc(tar, matches=True),
    }
