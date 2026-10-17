"""Differential test of the ``server.properties`` reader against a frozen copy.

Issue #3406 replaced the byte-at-a-time Python scan in
:func:`server_properties._parse` with bytes-level operations. The parity corpus
pins the Java behavior case by case; this pins the WHOLE behavior of the reader
it replaced, including what it did in corners no table names, by running both
over a generated corpus of structure-dense inputs and requiring identical
output -- key, value and byte span of every property.

The ``_frozen_*`` functions below are the reader exactly as it stood before
#3406, kept here as the reference. They are deliberately not maintained: if
the reader's semantics ever change on purpose, this file changes with them.
"""

from __future__ import annotations

import random

import pytest

from mc_server_dashboard_api.servers.domain import server_properties

# --- the reader before issue #3406, frozen ------------------------------------

_FROZEN_BLANKS = b" \t\f"
_FROZEN_HEX_DIGITS = "0123456789abcdefABCDEF"


def _frozen_natural_line(content: bytes, offset: int) -> tuple[bytes, int]:
    end = offset
    while end < len(content) and content[end] not in b"\n\r":
        end += 1
    if end >= len(content):
        return content[offset:end], end
    if content[end : end + 2] == b"\r\n":
        return content[offset:end], end + 2
    return content[offset:end], end + 1


def _frozen_ends_with_odd_backslash(line: bytes) -> bool:
    return (len(line) - len(line.rstrip(b"\\"))) % 2 == 1


def _frozen_unicode_escape(text: str, offset: int) -> tuple[str, int] | None:
    digits = text[offset : offset + 4]
    if len(digits) != 4 or not all(d in _FROZEN_HEX_DIGITS for d in digits):
        return None
    unit = int(digits, 16)
    if 0xD800 <= unit <= 0xDBFF and text[offset + 4 : offset + 6] == "\\u":
        low_digits = text[offset + 6 : offset + 10]
        if len(low_digits) == 4 and all(d in _FROZEN_HEX_DIGITS for d in low_digits):
            low = int(low_digits, 16)
            if 0xDC00 <= low <= 0xDFFF:
                return chr(0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)), 10
    return chr(unit), 4


def _frozen_load_convert(raw: bytes) -> str:
    text = raw.decode("latin-1")
    if "\\" not in text:
        return text
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        i += 1
        if char != "\\" or i >= len(text):
            out.append(char)
            continue
        char = text[i]
        i += 1
        if char == "u":
            escape = _frozen_unicode_escape(text, i)
            if escape is None:
                out.append("u")
            else:
                decoded, consumed = escape
                out.append(decoded)
                i += consumed
        else:
            out.append({"t": "\t", "r": "\r", "n": "\n", "f": "\f"}.get(char, char))
    return "".join(out)


def _frozen_split_key_value(line: bytes) -> tuple[str, str]:
    key_end = value_start = len(line)
    has_sep = False
    backslash = False
    for i in range(len(line)):
        char = line[i : i + 1]
        if not backslash and char in b"=:":
            key_end, value_start, has_sep = i, i + 1, True
            break
        if not backslash and char in _FROZEN_BLANKS:
            key_end, value_start = i, i + 1
            break
        backslash = char == b"\\" and not backslash
    while value_start < len(line):
        char = line[value_start : value_start + 1]
        if char not in _FROZEN_BLANKS:
            if has_sep or char not in b"=:":
                break
            has_sep = True
        value_start += 1
    return _frozen_load_convert(line[:key_end]), _frozen_load_convert(
        line[value_start:]
    )


def _frozen_parse(content: bytes) -> list[tuple[str, str, int, int]]:
    props: list[tuple[str, str, int, int]] = []
    offset = 0
    while offset < len(content):
        start = offset
        line, offset = _frozen_natural_line(content, offset)
        line = line.lstrip(_FROZEN_BLANKS)
        while line == b"\\" and offset < len(content):
            line, offset = _frozen_natural_line(content, offset)
            line = line.lstrip(_FROZEN_BLANKS)
        if line == b"\\" and content.endswith(b"\r\n"):
            continue
        if not line or line[:1] in (b"#", b"!"):
            continue
        if _frozen_ends_with_odd_backslash(line):
            logical = bytearray(line)
            continued = True
            while continued:
                del logical[-1]
                if offset >= len(content):
                    break
                continuation, offset = _frozen_natural_line(content, offset)
                continuation = continuation.lstrip(_FROZEN_BLANKS)
                logical += continuation
                continued = _frozen_ends_with_odd_backslash(continuation)
            line = bytes(logical)
        key, value = _frozen_split_key_value(line)
        props.append((key, value, start, offset))
    return props


# --- the generated corpus -------------------------------------------------------

# Every byte and multi-byte token the grammar gives meaning to, weighted toward
# the structural ones so a short input still crosses many rules: terminators in
# all three spellings, blanks, separators, comment markers, backslash runs,
# well-formed, malformed and surrogate ``\uXXXX`` escapes, and non-ASCII bytes.
_TOKENS: tuple[bytes, ...] = (
    b"\\",
    b"\\\\",
    b"\\\\\\",
    b"\n",
    b"\r",
    b"\r\n",
    b" ",
    b"\t",
    b"\f",
    b"=",
    b":",
    b"#",
    b"!",
    b"k",
    b"v",
    b"u",
    b"0",
    b"e9",
    b"\\u",
    b"\\u00e9",
    b"\\u00E",
    b"\\uD83D",
    b"\\uDE00",
    b"\\udbff",
    b"\\uDC00",
    b"\\t",
    b"\\n",
    b"\\=",
    b"\\ ",
    b"\xe9",
    b"\x00",
    b"\x0b",
    b"\x85",
    b"server-port",
    b"rcon.password",
)

_SEEDS = range(40)
_CASES_PER_SEED = 250


def _corpus(seed: int) -> list[bytes]:
    rng = random.Random(seed)
    return [
        b"".join(rng.choices(_TOKENS, k=rng.randint(0, 40)))
        for _ in range(_CASES_PER_SEED)
    ]


def _parsed(content: bytes) -> list[tuple[str, str, int, int]]:
    return [
        (prop.key, prop.value, prop.start, prop.end)
        for prop in server_properties._parse(content)
    ]


@pytest.mark.parametrize("seed", _SEEDS)
def test_parse_matches_the_frozen_reader(seed: int) -> None:
    for content in _corpus(seed):
        assert _parsed(content) == _frozen_parse(content), content


@pytest.mark.parametrize("seed", _SEEDS)
def test_parse_matches_the_frozen_reader_at_every_line_ending(seed: int) -> None:
    # The same corpus with every terminator respelled, so each input is also
    # read as all-LF, all-CR and all-CRLF -- the file-wide shape a real
    # server.properties has.
    for content in _corpus(seed):
        lf = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        for variant in (lf, lf.replace(b"\n", b"\r"), lf.replace(b"\n", b"\r\n")):
            assert _parsed(variant) == _frozen_parse(variant), variant


@pytest.mark.parametrize("seed", _SEEDS)
def test_load_convert_matches_the_frozen_decoder(seed: int) -> None:
    # Escape resolution on its own, where a value can end in a lone backslash
    # or a cut-short escape that a logical line never hands it.
    for content in _corpus(seed):
        assert server_properties._load_convert(content) == _frozen_load_convert(
            content
        ), content
