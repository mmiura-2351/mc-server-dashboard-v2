"""Tests for the ``server.properties`` server-port rewrite (issue #311)."""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.domain import server_properties
from mc_server_dashboard_api.servers.domain.server_properties import (
    PLATFORM_MANAGED_KEYS,
    RCON_PORT,
    ResourcePackProperties,
    _get_property,
    _parse,
    _raw_values,
    apply_overrides,
    apply_platform_properties,
    changed_platform_managed_keys,
    clear_resource_pack_properties,
    remove_keys,
    set_rcon_properties,
    set_resource_pack_properties,
    set_server_port,
)


def test_replaces_existing_server_port_line() -> None:
    content = b"motd=hi\nserver-port=25565\nmax-players=20\n"
    assert set_server_port(content, 25570) == (
        b"motd=hi\nserver-port=25570\nmax-players=20\n"
    )


def test_appends_when_no_server_port_line() -> None:
    content = b"motd=hi\nmax-players=20\n"
    assert set_server_port(content, 25570) == (
        b"motd=hi\nmax-players=20\nserver-port=25570\n"
    )


def test_empty_content_yields_only_the_port_line() -> None:
    assert set_server_port(b"", 25570) == b"server-port=25570\n"


def test_preserves_other_lines_and_order() -> None:
    content = b"#comment\nlevel-name=world\nserver-port=25565\nenable-rcon=true\n"
    assert set_server_port(content, 30000) == (
        b"#comment\nlevel-name=world\nserver-port=30000\nenable-rcon=true\n"
    )


def test_ignores_commented_server_port_line() -> None:
    # A commented-out server-port is not the live key; the real one is appended.
    content = b"#server-port=11111\nmotd=hi\n"
    assert set_server_port(content, 25570) == (
        b"#server-port=11111\nmotd=hi\nserver-port=25570\n"
    )


def test_rewrites_the_first_port_line_and_drops_the_rest() -> None:
    # Java's Properties.load is last-occurrence-wins, so leaving a second line
    # behind would hand the server a port the platform did not choose (#2811).
    content = b"server-port=1\nserver-port=2\n"
    assert set_server_port(content, 9) == b"server-port=9\n"


def test_no_trailing_newline_input_appends_without_adding_blank_line() -> None:
    content = b"motd=hi"
    # The file had no trailing newline; the append still lands on its own line and
    # a trailing newline is added (matching the seeded-file convention).
    assert set_server_port(content, 25570) == b"motd=hi\nserver-port=25570\n"


# --- RCON enforcement (issue #335) -----------------------------------------


def test_set_rcon_appends_all_keys_to_empty_content() -> None:
    out = set_rcon_properties(b"", password="s3cret")
    assert out == (
        f"enable-rcon=true\nrcon.port={RCON_PORT}\nrcon.password=s3cret\n".encode()
    )


def test_set_rcon_overwrites_disabled_enable_and_port() -> None:
    content = b"enable-rcon=false\nrcon.port=1234\nmotd=hi\n"
    out = set_rcon_properties(content, password="s3cret")
    assert out == (
        f"enable-rcon=true\nrcon.port={RCON_PORT}\nmotd=hi\nrcon.password=s3cret\n".encode()
    )


def test_set_rcon_preserves_existing_non_empty_password() -> None:
    content = b"enable-rcon=false\nrcon.password=known\nrcon.port=1234\n"
    out = set_rcon_properties(content, password="generated")
    # The non-empty existing password is preserved; enable/port are enforced.
    assert out == (
        f"enable-rcon=true\nrcon.password=known\nrcon.port={RCON_PORT}\n".encode()
    )


def test_set_rcon_fills_empty_existing_password() -> None:
    content = b"enable-rcon=false\nrcon.password=\nrcon.port=1234\n"
    out = set_rcon_properties(content, password="generated")
    assert out == (
        f"enable-rcon=true\nrcon.password=generated\nrcon.port={RCON_PORT}\n".encode()
    )


def test_set_rcon_preserves_other_lines_and_order() -> None:
    content = b"#comment\nlevel-name=world\nserver-port=25565\n"
    out = set_rcon_properties(content, password="s3cret")
    assert out == (
        b"#comment\nlevel-name=world\nserver-port=25565\n"
        + f"enable-rcon=true\nrcon.port={RCON_PORT}\nrcon.password=s3cret\n".encode()
    )


# --- resource pack properties (issue #1177) ----------------------------------

_RP_URL = "https://example.com/api/public/resource-packs/abc/pack.zip"
_RP_SHA1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"


def test_set_resource_pack_appends_all_keys_to_empty_content() -> None:
    out = set_resource_pack_properties(b"", url=_RP_URL, sha1=_RP_SHA1)
    assert (
        out
        == (
            f"resource-pack={_RP_URL}\n"
            f"resource-pack-sha1={_RP_SHA1}\n"
            f"require-resource-pack=false\n"
        ).encode()
    )


def test_set_resource_pack_with_require_true() -> None:
    out = set_resource_pack_properties(b"", url=_RP_URL, sha1=_RP_SHA1, require=True)
    assert b"require-resource-pack=true\n" in out


def test_set_resource_pack_with_prompt() -> None:
    out = set_resource_pack_properties(
        b"", url=_RP_URL, sha1=_RP_SHA1, prompt="Install this pack"
    )
    assert b"resource-pack-prompt=Install this pack\n" in out


def test_set_resource_pack_without_prompt_preserves_existing() -> None:
    content = b"resource-pack-prompt=Old prompt\nmotd=hi\n"
    out = set_resource_pack_properties(content, url=_RP_URL, sha1=_RP_SHA1)
    # prompt=None leaves the existing prompt line untouched
    assert b"resource-pack-prompt=Old prompt\n" in out


def test_set_resource_pack_replaces_existing_keys() -> None:
    content = (
        b"resource-pack=old-url\n"
        b"resource-pack-sha1=old-sha\n"
        b"require-resource-pack=true\n"
        b"motd=hi\n"
    )
    out = set_resource_pack_properties(
        content, url=_RP_URL, sha1=_RP_SHA1, require=False
    )
    assert (
        out
        == (
            f"resource-pack={_RP_URL}\n"
            f"resource-pack-sha1={_RP_SHA1}\n"
            f"require-resource-pack=false\n"
            f"motd=hi\n"
        ).encode()
    )


def test_set_resource_pack_preserves_other_lines() -> None:
    content = b"#comment\nserver-port=25565\nlevel-name=world\n"
    out = set_resource_pack_properties(content, url=_RP_URL, sha1=_RP_SHA1)
    assert out.startswith(b"#comment\nserver-port=25565\nlevel-name=world\n")


def test_clear_resource_pack_removes_all_four_keys() -> None:
    content = (
        b"motd=hi\n"
        b"resource-pack=some-url\n"
        b"resource-pack-sha1=some-sha\n"
        b"require-resource-pack=true\n"
        b"resource-pack-prompt=Hi there\n"
        b"max-players=20\n"
    )
    out = clear_resource_pack_properties(content)
    assert out == b"motd=hi\nmax-players=20\n"


def test_clear_resource_pack_on_empty_content() -> None:
    out = clear_resource_pack_properties(b"")
    assert out == b"\n"


def test_clear_resource_pack_preserves_other_lines() -> None:
    content = b"motd=hi\nserver-port=25565\n"
    out = clear_resource_pack_properties(content)
    assert out == b"motd=hi\nserver-port=25565\n"


def test_clear_resource_pack_ignores_commented_keys() -> None:
    content = b"#resource-pack=url\nresource-pack=real-url\nmotd=hi\n"
    out = clear_resource_pack_properties(content)
    assert out == b"#resource-pack=url\nmotd=hi\n"


# --- apply_overrides (issue #1209) -------------------------------------------


def test_apply_overrides_appends_new_keys_to_empty_content() -> None:
    out = apply_overrides(b"", {"motd": "Hello World", "pvp": "true"})
    assert out == b"motd=Hello World\npvp=true\n"


def test_apply_overrides_rewrites_existing_keys_in_place() -> None:
    content = b"motd=old\nmax-players=20\n"
    out = apply_overrides(content, {"motd": "new"})
    assert out == b"motd=new\nmax-players=20\n"


def test_apply_overrides_mixes_rewrite_and_append() -> None:
    content = b"motd=old\n"
    out = apply_overrides(content, {"motd": "new", "pvp": "false"})
    assert out == b"motd=new\npvp=false\n"


def test_apply_overrides_preserves_other_lines() -> None:
    content = b"#comment\nserver-port=25565\nmotd=hi\n"
    out = apply_overrides(content, {"motd": "bye"})
    assert out == b"#comment\nserver-port=25565\nmotd=bye\n"


def test_apply_overrides_empty_dict_is_noop() -> None:
    content = b"motd=hi\n"
    assert apply_overrides(content, {}) == b"motd=hi\n"


# --- write-side escaping (issue #2819) ----------------------------------------


def test_apply_overrides_escapes_a_newline_in_a_value() -> None:
    # The injection #2819 names: an unescaped newline in an override value ends
    # the line, so the rest of the value becomes property lines of its own --
    # below the platform's own rcon.password, which is the one Java then reads.
    content = b"rcon.password=tok\nmotd=hi\n"
    out = apply_overrides(content, {"motd": "hi\nrcon.password=evil"})
    assert _get_property(out, "motd") == "hi\nrcon.password=evil"
    assert _raw_values(out, "rcon.password") == ["tok"]


def test_apply_overrides_writes_the_escaped_spelling() -> None:
    # The written line, pinned: one logical line carrying the escape, not two.
    assert apply_overrides(b"", {"motd": "a\nb"}) == rb"motd=a\nb" + b"\n"


def test_apply_overrides_escapes_a_trailing_backslash_in_a_value() -> None:
    # An odd trailing backslash run continues the logical line, so an unescaped
    # one would swallow the next property line into the value.
    content = b"motd=hi\nmax-players=20\n"
    out = apply_overrides(content, {"motd": "C:\\path\\"})
    assert _get_property(out, "motd") == "C:\\path\\"
    assert _get_property(out, "max-players") == "20"


@pytest.mark.parametrize(
    "value",
    [
        "=x",
        ":x",
        "#x",
        "!x",
        " x",
        "  x",
        "\tx",
        "\fx",
        "a\rb",
        "a\tb",
        "a\\b",
        "",
    ],
)
def test_apply_overrides_values_round_trip_through_a_java_parse(value: str) -> None:
    out = apply_overrides(b"motd=old\nmax-players=20\n", {"motd": value})
    assert _get_property(out, "motd") == value
    assert _get_property(out, "max-players") == "20"


@pytest.mark.parametrize("key", ["a=b", "a:b", "a b", "#a", "!a", "a\\b", "a\nb", "日"])
def test_apply_overrides_keys_round_trip_through_a_java_parse(key: str) -> None:
    out = apply_overrides(b"motd=hi\n", {key: "v"})
    assert _get_property(out, key) == "v"
    assert _get_property(out, "motd") == "hi"


@pytest.mark.parametrize("key", sorted(PLATFORM_MANAGED_KEYS))
def test_no_override_value_can_add_a_platform_managed_key(key: str) -> None:
    # The acceptance criterion of #2819: whatever the value carries, the guard's
    # own reading of the platform's keys is untouched by the write.
    current = apply_platform_properties(
        b"",
        game_port=25565,
        rcon_password="tok",
        resource_pack=ResourcePackProperties(
            url="https://example.test/pack.zip",
            sha1="a" * 40,
            require=True,
            prompt="Use it",
        ),
    )
    out = apply_overrides(current, {"motd": f"hi\n{key}=evil"})
    assert changed_platform_managed_keys(current, out) == []


def test_apply_overrides_leaves_an_ordinary_value_readable() -> None:
    # Only what Java would misread is escaped: a non-leading ``:`` is literal in
    # a value, so a URL keeps the spelling an operator reading the file expects.
    out = apply_overrides(b"", {"resource-pack": "https://example.test/pack.zip"})
    assert out == b"resource-pack=https://example.test/pack.zip\n"


# --- write-side encoding (issue #2820) ----------------------------------------


def test_resource_pack_prompt_round_trips_a_non_latin1_value() -> None:
    # The case #2820 names: a Japanese prompt encoded UTF-8 into a file the Java
    # server reads as latin-1 reached it mojibaked.
    out = set_resource_pack_properties(
        b"", url=_RP_URL, sha1=_RP_SHA1, prompt="リソースパック"
    )
    assert _get_property(out, "resource-pack-prompt") == "リソースパック"


def test_a_non_latin1_value_is_written_as_unicode_escapes() -> None:
    # The spelling java.util.Properties.store emits, pinned: the written line
    # stays pure ASCII and the escape is what carries the code point.
    assert apply_overrides(b"", {"motd": "日本"}) == rb"motd=\u65E5\u672C" + b"\n"


def test_a_latin1_value_is_written_as_a_unicode_escape() -> None:
    # Properties.store's threshold, not "whatever latin-1 can hold": a raw 0xE9
    # byte is the right e-acute to a latin-1 reader alone, while the escape is
    # the right one to every reader of the file.
    out = apply_overrides(b"", {"motd": "café"})
    assert out == rb"motd=caf\u00E9" + b"\n"
    assert _get_property(out, "motd") == "café"


def test_a_written_line_carries_no_non_ascii_byte() -> None:
    # What makes a platform write survivable end to end: the webui reads the file
    # through a UTF-8 decoder, so a raw non-ASCII byte would come back as U+FFFD
    # and saving the text again would report resource-pack-prompt -- a key the
    # platform owns -- as changed, refusing the write with a 409.
    out = set_resource_pack_properties(
        b"", url=_RP_URL, sha1=_RP_SHA1, prompt="Télécharge パック"
    )
    assert out.isascii()


def test_an_astral_value_is_written_as_a_surrogate_pair() -> None:
    # Above the BMP, Properties.store emits one escape per UTF-16 unit; a single
    # five-digit escape would read back as four digits plus a stray literal.
    # _parse resolves each half to a lone surrogate -- Java's own reader pairs
    # them back into the character, which is what the server sees.
    out = apply_overrides(b"", {"motd": "\U0001f600"})
    assert out == rb"motd=\uD83D\uDE00" + b"\n"


def test_a_lone_surrogate_value_round_trips_without_raising() -> None:
    # An unpaired surrogate is outside printable ASCII too, so it is written as
    # the escape Properties.store emits, instead of reaching a strict encoder.
    out = apply_overrides(b"", {"motd": "\ud800"})
    assert out == rb"motd=\uD800" + b"\n"
    assert _get_property(out, "motd") == "\ud800"


def test_ascii_platform_writes_are_byte_identical() -> None:
    # #2820 changes the encoding, not the spelling: a platform-written value in
    # printable ASCII -- 0x20-0x7E, plus the characters _ESCAPES already spelled
    # as escapes -- lands as exactly the bytes it did before. Outside that set
    # the spelling does change, to the \uXXXX escape Properties.store emits, which
    # Java reads identically.
    out = apply_platform_properties(
        b"",
        game_port=25565,
        rcon_password="tok",
        resource_pack=ResourcePackProperties(
            url=_RP_URL, sha1=_RP_SHA1, require=True, prompt="Use this pack"
        ),
    )
    assert out == (
        f"server-port=25565\n"
        f"enable-rcon=true\n"
        f"rcon.port={RCON_PORT}\n"
        f"rcon.password=tok\n"
        f"resource-pack={_RP_URL}\n"
        f"resource-pack-sha1={_RP_SHA1}\n"
        f"require-resource-pack=true\n"
        f"resource-pack-prompt=Use this pack\n"
    ).encode("ascii")


# --- remove_keys (issue #1242) ------------------------------------------------


def test_remove_keys_deletes_matching_lines() -> None:
    content = b"motd=hi\npvp=true\nmax-players=20\n"
    out = remove_keys(content, {"pvp"})
    assert out == b"motd=hi\nmax-players=20\n"


def test_remove_keys_preserves_other_lines_and_comments() -> None:
    content = b"#comment\nserver-port=25565\nmotd=hi\npvp=true\n"
    out = remove_keys(content, {"motd"})
    assert out == b"#comment\nserver-port=25565\npvp=true\n"


def test_remove_keys_multiple_keys() -> None:
    content = b"motd=hi\npvp=true\nmax-players=20\n"
    out = remove_keys(content, {"motd", "max-players"})
    assert out == b"pvp=true\n"


def test_remove_keys_absent_key_is_noop() -> None:
    content = b"motd=hi\n"
    out = remove_keys(content, {"pvp"})
    assert out == b"motd=hi\n"


def test_remove_keys_empty_set_is_noop() -> None:
    content = b"motd=hi\n"
    out = remove_keys(content, set())
    assert out == b"motd=hi\n"


# --- platform-managed keys (issue #2623) --------------------------------------


def test_platform_managed_keys_covers_the_port_and_rcon_keys() -> None:
    # The keys the platform owns: the tracked bind port (#311) and the RCON
    # triple the worker reaches the server with (#335).
    assert {
        "server-port",
        "enable-rcon",
        "rcon.port",
        "rcon.password",
    } <= PLATFORM_MANAGED_KEYS


def test_platform_managed_keys_covers_the_resource_pack_keys() -> None:
    # Written by set_resource_pack_properties / clear_resource_pack_properties
    # from the assignment row (#1177, #1253), so they are platform-owned too.
    assert {
        "resource-pack",
        "resource-pack-sha1",
        "require-resource-pack",
        "resource-pack-prompt",
    } <= PLATFORM_MANAGED_KEYS


def test_unchanged_platform_keys_report_no_change() -> None:
    current = b"server-port=25565\nrcon.password=tok\nmotd=hi\n"
    incoming = b"server-port=25565\nrcon.password=tok\nmotd=bye\n"
    assert changed_platform_managed_keys(current, incoming) == []


def test_changed_platform_key_is_reported() -> None:
    current = b"server-port=25565\nrcon.password=tok\n"
    incoming = b"server-port=25999\nrcon.password=tok\n"
    assert changed_platform_managed_keys(current, incoming) == ["server-port"]


def test_removed_platform_key_is_reported() -> None:
    # Dropping the line is a change: the worker loses the credential (#2623).
    current = b"server-port=25565\nrcon.password=tok\n"
    incoming = b"server-port=25565\n"
    assert changed_platform_managed_keys(current, incoming) == ["rcon.password"]


def test_added_platform_key_is_reported() -> None:
    # A legacy file with no RCON line must not gain one through a user edit.
    current = b"server-port=25565\n"
    incoming = b"server-port=25565\nrcon.password=evil\n"
    assert changed_platform_managed_keys(current, incoming) == ["rcon.password"]


def test_appended_duplicate_platform_key_is_reported() -> None:
    # Java's Properties.load is last-occurrence-wins, so leaving the original
    # line intact and appending a second one still changes what the server reads.
    current = b"rcon.password=tok\n"
    incoming = b"rcon.password=tok\nrcon.password=evil\n"
    assert changed_platform_managed_keys(current, incoming) == ["rcon.password"]


def test_whitespace_around_the_separator_is_not_a_change() -> None:
    # Java skips whitespace around the separator and ends a line at a CR, so
    # neither a reformatting edit nor a CRLF round-trip changes what it reads.
    current = b"server-port=25565\r\n"
    incoming = b"server-port =  25565\n"
    assert changed_platform_managed_keys(current, incoming) == []


def test_trailing_whitespace_on_a_platform_value_is_a_change() -> None:
    # Java keeps trailing whitespace in the value, so "25565 " is NOT "25565".
    # The guard reports what the server would really read, not a tidied version.
    current = b"server-port=25565\n"
    incoming = b"server-port=25565 \n"
    assert changed_platform_managed_keys(current, incoming) == ["server-port"]


def test_commented_out_platform_key_counts_as_removal() -> None:
    current = b"rcon.password=tok\n"
    incoming = b"#rcon.password=tok\n"
    assert changed_platform_managed_keys(current, incoming) == ["rcon.password"]


def test_changed_platform_keys_are_reported_sorted() -> None:
    current = b"server-port=25565\nrcon.password=tok\n"
    incoming = b"server-port=25999\nrcon.password=evil\n"
    assert changed_platform_managed_keys(current, incoming) == [
        "rcon.password",
        "server-port",
    ]


def test_non_platform_keys_are_never_reported() -> None:
    current = b"motd=hi\nmax-players=20\n"
    incoming = b"motd=bye\n"
    assert changed_platform_managed_keys(current, incoming) == []


def test_non_utf8_bytes_are_compared_without_decoding() -> None:
    # A server.properties carrying a latin-1 motd is not this guard's business to
    # reject: comparing must never raise UnicodeDecodeError (issue #2623).
    current = b"server-port=25565\nmotd=caf\xe9\n"
    incoming = b"server-port=25565\nmotd=caf\xe9 bar\n"
    assert changed_platform_managed_keys(current, incoming) == []


def test_non_utf8_platform_value_change_is_still_detected() -> None:
    # Two DIFFERENT invalid byte sequences must not collapse into one another,
    # which a lossy decode would do.
    current = b"rcon.password=\xff\xfe\n"
    incoming = b"rcon.password=\xfe\xff\n"
    assert changed_platform_managed_keys(current, incoming) == ["rcon.password"]


def test_the_guard_parses_each_side_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Asking _parse for one key at a time re-read both whole files once per
    # platform-managed key -- sixteen passes over a file the guard needs to read
    # twice. _parse walks the content byte by byte, so on an oversized root file
    # (restore/import, or one predating the #2809 cap) that is seconds of a
    # worker thread (issue #2831).
    parsed: list[bytes] = []
    real_parse = server_properties._parse

    def counting_parse(content: bytes) -> list[server_properties._Property]:
        parsed.append(content)
        return real_parse(content)

    monkeypatch.setattr(server_properties, "_parse", counting_parse)
    current = b"server-port=25565\nrcon.password=tok\n"
    incoming = b"server-port=25999\nrcon.password=tok\n"
    assert changed_platform_managed_keys(current, incoming) == ["server-port"]
    assert parsed == [current, incoming]


# --- apply_platform_properties (issue #2621) --------------------------------


def test_apply_platform_properties_covers_every_managed_key() -> None:
    # The re-apply is what import and restore lean on, so it must leave no
    # platform-managed key carrying the republished file's value.
    content = (
        b"server-port=25565\nenable-rcon=false\nrcon.port=1234\n"
        b"rcon.password=old\nresource-pack=https://old/pack.zip\n"
        b"resource-pack-sha1=old-sha\nrequire-resource-pack=false\n"
        b"resource-pack-prompt=old prompt\nmotd=hi\n"
    )
    result = apply_platform_properties(
        content,
        game_port=26590,
        rcon_password="fresh",
        resource_pack=ResourcePackProperties(
            url="https://new/pack.zip", sha1="new-sha", require=True, prompt="new"
        ),
    )
    props = dict(
        line.split("=", 1) for line in result.decode().splitlines() if "=" in line
    )
    assert PLATFORM_MANAGED_KEYS <= props.keys()
    assert props["server-port"] == "26590"
    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == str(RCON_PORT)
    assert props["resource-pack"] == "https://new/pack.zip"
    assert props["resource-pack-sha1"] == "new-sha"
    assert props["require-resource-pack"] == "true"
    assert props["resource-pack-prompt"] == "new"
    assert props["motd"] == "hi"


def test_apply_platform_properties_keeps_a_working_rcon_password() -> None:
    # The file is rcon.password's only source of truth (#335), so a republished
    # file that carries a working credential keeps it.
    result = apply_platform_properties(
        b"rcon.password=known-secret\n",
        game_port=26590,
        rcon_password="fresh",
        resource_pack=None,
    )
    assert b"rcon.password=known-secret\n" in result


def test_apply_platform_properties_leaves_the_port_alone_without_a_tracked_one() -> (
    None
):
    # A legacy row predating port tracking (game_port NULL) owns no port, so the
    # file's own value stands rather than being replaced by a guess.
    result = apply_platform_properties(
        b"server-port=25565\n",
        game_port=None,
        rcon_password="fresh",
        resource_pack=None,
    )
    assert b"server-port=25565\n" in result


def test_apply_platform_properties_clears_the_pack_keys_when_unassigned() -> None:
    content = (
        b"resource-pack=https://old/pack.zip\nresource-pack-sha1=old-sha\n"
        b"require-resource-pack=true\nresource-pack-prompt=old\n"
    )
    result = apply_platform_properties(
        content, game_port=26590, rcon_password="fresh", resource_pack=None
    )
    assert b"resource-pack" not in result


def test_apply_platform_properties_removes_the_prompt_when_the_row_has_none() -> None:
    # An assignment with no prompt says "no prompt", so the republished file's own
    # prompt must go -- unlike an assign, which leaves an unspecified prompt alone.
    result = apply_platform_properties(
        b"resource-pack-prompt=old prompt\n",
        game_port=26590,
        rcon_password="fresh",
        resource_pack=ResourcePackProperties(
            url="https://new/pack.zip", sha1="new-sha", require=False, prompt=None
        ),
    )
    assert b"resource-pack-prompt" not in result


def test_rewrites_preserve_a_non_utf8_line() -> None:
    # A latin-1 motd is an ordinary server.properties (issue #2623 made the guard
    # byte-level for it); the rewrites must round-trip those bytes rather than
    # raising, or a restore of such a file would 503 forever (issue #2621).
    content = b"motd=caf\xe9\nserver-port=25565\n"
    assert set_server_port(content, 26590) == b"motd=caf\xe9\nserver-port=26590\n"


def test_apply_platform_properties_preserves_a_non_utf8_line() -> None:
    result = apply_platform_properties(
        b"motd=caf\xe9\nserver-port=25565\n",
        game_port=26590,
        rcon_password="fresh",
        resource_pack=None,
    )
    assert b"motd=caf\xe9\n" in result
    assert b"server-port=26590\n" in result


# --- Java Properties.load parity (issue #2811) --------------------------------

# The parse table mirrored one-for-one by the Worker's
# worker/internal/javaproperties/javaproperties_test.go::parityCases. Same input,
# same parse -- that mirroring is the evidence that the platform-key guard and
# the Worker read a server.properties alike. Keep the two tables in sync: a case
# added here but not there leaves the invariant unpinned.
PARITY_CASES: list[tuple[str, bytes, dict[str, str]]] = [
    ("equals separator", b"server-port=25599\n", {"server-port": "25599"}),
    ("colon separator", b"server-port:25599\n", {"server-port": "25599"}),
    ("whitespace separator", b"server-port 25599\n", {"server-port": "25599"}),
    (
        "separator with surrounding whitespace",
        b"server-port = 25599\n",
        {"server-port": "25599"},
    ),
    (
        "whitespace then colon separator",
        b"server-port : 25599\n",
        {"server-port": "25599"},
    ),
    ("tab separator", b"server-port\t25599\n", {"server-port": "25599"}),
    (
        "leading whitespace before the key",
        b"   server-port=25599\n",
        {"server-port": "25599"},
    ),
    (
        "a second separator belongs to the value",
        b"server-port==25599\n",
        {"server-port": "=25599"},
    ),
    (
        "trailing whitespace is part of the value",
        b"server-port=25599  \n",
        {"server-port": "25599  "},
    ),
    (
        "a key with no separator has an empty value",
        b"server-port\n",
        {"server-port": ""},
    ),
    (
        "a hash comment is skipped",
        b"#server-port=1\nserver-port=25599\n",
        {"server-port": "25599"},
    ),
    (
        "a bang comment is skipped",
        b"!server-port=1\nserver-port=25599\n",
        {"server-port": "25599"},
    ),
    (
        "a comment does not continue on a trailing backslash",
        b"#server-port=1\\\nserver-port=25599\n",
        {"server-port": "25599"},
    ),
    (
        "blank lines are skipped",
        b"\n   \nserver-port=25599\n",
        {"server-port": "25599"},
    ),
    (
        "a backslash continues onto the next line",
        b"rcon.password=one\\\n  two\n",
        {"rcon.password": "onetwo"},
    ),
    (
        "an even trailing backslash run does not continue",
        b"rcon.password=one\\\\\nmotd=hi\n",
        {"rcon.password": "one\\", "motd": "hi"},
    ),
    (
        "a continuation line is never a comment",
        b"rcon.password=one\\\n#two\n",
        {"rcon.password": "one#two"},
    ),
    (
        "a blank continuation line ends the value",
        b"rcon.password=one\\\n\nmotd=hi\n",
        {"rcon.password": "one", "motd": "hi"},
    ),
    (
        "an escaped dot in the key",
        rb"rcon\.password=tok" + b"\n",
        {"rcon.password": "tok"},
    ),
    (
        "an escaped separator in the key",
        rb"rcon\=password=tok" + b"\n",
        {"rcon=password": "tok"},
    ),
    ("an escaped hash in the key", rb"a\#b=tok" + b"\n", {"a#b": "tok"}),
    ("an escaped space in the key", rb"a\ b=tok" + b"\n", {"a b": "tok"}),
    ("an escaped bang in the key", rb"a\!b=tok" + b"\n", {"a!b": "tok"}),
    (
        "a unicode escape in the key",
        rb"\u0072con.password=tok" + b"\n",
        {"rcon.password": "tok"},
    ),
    ("a unicode escape in the value", rb"motd=caf\u00e9" + b"\n", {"motd": "café"}),
    ("an escaped colon in the value", rb"motd=a\:b" + b"\n", {"motd": "a:b"}),
    ("an escaped leading hash in the value", rb"motd=\#hi" + b"\n", {"motd": "#hi"}),
    ("an escaped leading space in the value", rb"motd=\ hi" + b"\n", {"motd": " hi"}),
    (
        "control-character escapes in the value",
        rb"motd=a\tb\nc" + b"\n",
        {"motd": "a\tb\nc"},
    ),
    ("a carriage-return escape in the value", rb"motd=a\rb" + b"\n", {"motd": "a\rb"}),
    ("a form-feed escape in the value", rb"motd=a\fb" + b"\n", {"motd": "a\fb"}),
    (
        "the last occurrence wins",
        b"server-port=1\nserver-port:2\n",
        {"server-port": "2"},
    ),
    (
        "CRLF terminates a line",
        b"server-port=25599\r\nmotd=hi\r\n",
        {"server-port": "25599", "motd": "hi"},
    ),
    (
        "a lone CR terminates a line",
        b"server-port=25599\rmotd=hi\r",
        {"server-port": "25599", "motd": "hi"},
    ),
    (
        "a final line without a terminator is parsed",
        b"server-port=25599",
        {"server-port": "25599"},
    ),
    ("a trailing lone backslash at EOF is dropped", b"motd=hi\\", {"motd": "hi"}),
    ("latin-1 bytes decode byte-for-byte", b"motd=caf\xe9\n", {"motd": "café"}),
    (
        "a malformed unicode escape keeps the u literal",
        rb"motd=a\uZZZZb" + b"\n",
        {"motd": "auZZZZb"},
    ),
    ("an empty file has no properties", b"", {}),
]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(content, expected, id=name)
        for name, content, expected in PARITY_CASES
    ],
)
def test_parses_like_java_properties_load(
    content: bytes, expected: dict[str, str]
) -> None:
    # Last occurrence wins, matching java.util.Properties.load.
    assert {prop.key: prop.value for prop in _parse(content)} == expected


# --- the guard sees every Java spelling (issue #2811) -------------------------


def test_appended_colon_form_duplicate_is_reported() -> None:
    # The bypass #2811 names: leave the original line intact and append the key
    # again with a colon. Java reads "evil"; so must the guard.
    current = b"rcon.password=tok\n"
    incoming = b"rcon.password=tok\nrcon.password:evil\n"
    assert changed_platform_managed_keys(current, incoming) == ["rcon.password"]


def test_appended_whitespace_form_duplicate_is_reported() -> None:
    current = b"server-port=25565\n"
    incoming = b"server-port=25565\nserver-port 25999\n"
    assert changed_platform_managed_keys(current, incoming) == ["server-port"]


def test_appended_escaped_key_duplicate_is_reported() -> None:
    current = b"rcon.password=tok\n"
    incoming = b"rcon.password=tok\n" + rb"rcon\.password=evil" + b"\n"
    assert changed_platform_managed_keys(current, incoming) == ["rcon.password"]


def test_appended_unicode_escaped_key_duplicate_is_reported() -> None:
    current = b"rcon.password=tok\n"
    incoming = b"rcon.password=tok\n" + rb"\u0072con.password=evil" + b"\n"
    assert changed_platform_managed_keys(current, incoming) == ["rcon.password"]


def test_continuation_line_value_change_is_reported() -> None:
    # The value spills onto a second line; changing that half changes the key.
    current = b"rcon.password=to\\\nk\n"
    incoming = b"rcon.password=to\\\nken\n"
    assert changed_platform_managed_keys(current, incoming) == ["rcon.password"]


def test_bang_commenting_out_a_platform_key_counts_as_removal() -> None:
    # "!" is a comment marker for Properties.load just as "#" is.
    current = b"rcon.password=tok\n"
    incoming = b"!rcon.password=tok\n"
    assert changed_platform_managed_keys(current, incoming) == ["rcon.password"]


def test_respelling_a_platform_line_without_changing_its_value_is_allowed() -> None:
    # Java reads the same value out of either spelling, so nothing changed for
    # the server -- the guard must not refuse a write that changes nothing.
    current = b"server-port=25565\n"
    incoming = b"server-port:25565\n"
    assert changed_platform_managed_keys(current, incoming) == []


# --- clearing reaches every Java spelling (issue #2811) -----------------------


def test_clear_resource_pack_removes_a_colon_form_line() -> None:
    # An untrusted archive's respelled pack line must not survive the clear that
    # import/restore runs, or the Java server still serves it (issue #2621).
    content = b"motd=hi\nresource-pack:http://attacker/pack.zip\n"
    assert clear_resource_pack_properties(content) == b"motd=hi\n"


def test_clear_resource_pack_removes_a_whitespace_form_line() -> None:
    content = b"motd=hi\nresource-pack http://attacker/pack.zip\n"
    assert clear_resource_pack_properties(content) == b"motd=hi\n"


def test_clear_resource_pack_removes_a_continued_line_whole() -> None:
    # A backslash continuation is ONE property to Java; leaving its tail behind
    # would both keep the pack and corrupt the following line.
    content = b"motd=hi\nresource-pack=http://attacker/\\\npack.zip\nmax-players=20\n"
    assert clear_resource_pack_properties(content) == b"motd=hi\nmax-players=20\n"


def test_clear_resource_pack_removes_every_occurrence() -> None:
    content = (
        b"resource-pack=http://one/pack.zip\nmotd=hi\n"
        b"resource-pack:http://two/pack.zip\n"
    )
    assert clear_resource_pack_properties(content) == b"motd=hi\n"


def test_remove_keys_removes_a_colon_form_line() -> None:
    assert remove_keys(b"motd:hi\npvp=true\n", {"motd"}) == b"pvp=true\n"


def test_apply_platform_properties_clears_a_colon_form_pack_line() -> None:
    # The import/restore path: no assignment means every pack line goes, in
    # whatever spelling the archive used.
    result = apply_platform_properties(
        b"resource-pack:http://attacker/pack.zip\n",
        game_port=26590,
        rcon_password="fresh",
        resource_pack=None,
    )
    assert b"resource-pack" not in result


# --- writes leave no respelled duplicate behind (issue #2811) -----------------


def test_set_server_port_drops_a_respelled_duplicate() -> None:
    # Rewriting the canonical line while a colon-form one survived below it would
    # hand the server the archive's port under Java's last-occurrence rule.
    content = b"server-port=25565\nmotd=hi\nserver-port:25999\n"
    assert set_server_port(content, 26590) == b"server-port=26590\nmotd=hi\n"


def test_set_server_port_replaces_a_colon_form_line_in_place() -> None:
    content = b"motd=hi\nserver-port:25999\n"
    assert set_server_port(content, 26590) == b"motd=hi\nserver-port=26590\n"


def test_set_resource_pack_drops_a_respelled_duplicate() -> None:
    content = (
        b"resource-pack=https://old/pack.zip\nresource-pack:http://attacker/pack.zip\n"
    )
    out = set_resource_pack_properties(content, url=_RP_URL, sha1=_RP_SHA1)
    # Only the platform's own line survives: a colon-form one below it would be
    # what Java reads.
    assert _raw_values(out, "resource-pack") == [_RP_URL]


def test_set_rcon_keeps_a_colon_form_password() -> None:
    # rcon.password is read out of the file by the same Java rules the worker
    # uses, so a colon-form credential is a live one and is preserved as it is.
    out = set_rcon_properties(b"rcon.password:known\n", password="generated")
    assert _raw_values(out, "rcon.password") == ["known"]


# --- a degenerate override key stays its own key (issue #2822) ----------------


@pytest.mark.parametrize(
    "key",
    [
        "server-port ",
        " server-port",
        r"rcon\.password",
        "server-port:",
        "server-port=",
        "server-port\t",
        "server-port\n",
    ],
)
def test_a_degenerate_override_key_is_a_key_of_its_own(key: str) -> None:
    # Written verbatim, each spelling would collapse onto the platform-managed
    # key it is spelled after -- Properties.load ends a key at a blank, ":", "="
    # or the line's end, strips the blanks leading it, and resolves its escapes.
    # That is the hole #2822 names. _escape_key closes it: the key is written so
    # Java reads it back as itself, which keeps all three of these true at once.
    current = apply_platform_properties(
        b"", game_port=25565, rcon_password="tok", resource_pack=None
    )
    out = apply_overrides(current, {key: "one"})
    # It does not alias the platform key.
    assert changed_platform_managed_keys(current, out) == []
    # It re-finds its own line on the next write instead of appending a
    # duplicate no later write could reach (the #1242 half).
    out = apply_overrides(out, {key: "two"})
    assert _raw_values(out, key) == ["two"]
    # And it can still be cleared.
    assert _raw_values(remove_keys(out, {key}), key) == []


# --- checked against the reference java.util.Properties (issue #2820) ---------

# The escapes written above are only right if the reference implementation reads
# them back as what was submitted, and no Python reimplementation can settle
# that -- _parse is the same author's reading of the same spec. So when a JDK is
# on PATH the writer is checked against java.util.Properties itself; without one
# this skips and the _parse pins stand on their own (this module's CI is
# Python-only).
#
# The probe prints, per file, every UTF-16 unit of the key and of the value as
# hex -- what a Java string is made of -- so an astral code point and a lone
# surrogate survive the comparison instead of collapsing on the way out. Fields
# are space-separated, which no hex dump or file name here contains, and that
# keeps the Java source free of escapes of its own.
_PROBE_JAVA = """\
import java.io.FileInputStream;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.stream.Collectors;
import java.util.stream.Stream;

public class Probe {
    static String hex(String s) {
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            out.append(String.format("%04x", (int) s.charAt(i)));
        }
        return out.toString();
    }

    public static void main(String[] args) throws Exception {
        List<Path> files;
        try (Stream<Path> paths = Files.list(Paths.get(args[0]))) {
            files = paths.sorted().collect(Collectors.toList());
        }
        StringBuilder out = new StringBuilder();
        for (Path file : files) {
            Properties props = new Properties();
            try (InputStream in = new FileInputStream(file.toFile())) {
                props.load(in);
            }
            out.append(file.getFileName().toString());
            if (props.size() == 1) {
                Map.Entry<Object, Object> e = props.entrySet().iterator().next();
                out.append(' ').append(hex((String) e.getKey()));
                out.append(' ').append(hex((String) e.getValue()));
            } else {
                out.append(" properties:").append(props.size());
            }
            out.append(System.lineSeparator());
        }
        System.out.print(out);
    }
}
"""


def _java_runs_source_files() -> bool:
    """True when a JDK able to execute a ``.java`` source file is on PATH.

    A bare ``java`` is not enough: source-file mode compiles in memory, so a
    runtime without ``jdk.compiler`` would fail the run rather than skip it.
    """

    if shutil.which("java") is None:
        return False
    modules = subprocess.run(
        ["java", "--list-modules"], capture_output=True, text=True, timeout=60
    )
    return "jdk.compiler" in modules.stdout


_JDK_ON_PATH = _java_runs_source_files()


def _utf16_units(text: str) -> str:
    """Return *text* as the hex of its UTF-16 units, which a Java string is."""

    return text.encode("utf-16-be", "surrogatepass").hex()


def _reference_cases() -> list[tuple[str, str]]:
    """Return the (key, value) pairs the reference check writes and reads back.

    The fixed pairs are the ones the escaping was written for (#2819's injection
    and leading-character cases, #2820's encoding ones); the drawn pairs mix
    those alphabets so the two rules meet in every combination.
    """

    keys = [
        "motd",
        "resource-pack-prompt",
        "a=b",
        "a:b",
        "a b",
        "#a",
        "!a",
        "a\\b",
        "a\nb",
        "\u65e5",
        "\U0001f600",
        "\ud800",
    ]
    values = [
        "",
        "hi",
        "25565",
        "https://example.test/pack.zip",
        "hi\nrcon.password=evil",
        "C:\\path\\",
        " leading",
        "=lead",
        ":lead",
        "#lead",
        "!lead",
        "trailing ",
        "a\tb\fc\rd",
        "\\",
        "caf\u00e9",
        "\u65e5\u672c\u8a9e",
        "\U0001f600",
        "\ud800",
        "\udfff",
    ]
    pools = [
        "abcXYZ019 -_./",
        "=:#!\\ \t\f\n\r",
        "".join(chr(code) for code in range(0x80, 0x100)),
        "\u65e5\u672c\u8a9e\u00e9\u00df\u03a9\u0416\ud55c",
        "".join(chr(code) for code in (0x1F600, 0x10000, 0x10FFFF, 0x2070E)),
        "".join(chr(code) for code in (0xD800, 0xDBFF, 0xDC00, 0xDFFF)),
    ]
    cases = [(key, value) for key in keys for value in values]
    rng = random.Random(2820)
    for _ in range(1500):
        pool = rng.choice(pools) + rng.choice(pools)
        key = "".join(rng.choice(pool) for _ in range(rng.randint(1, 8)))
        value = "".join(rng.choice(pool) for _ in range(rng.randint(0, 12)))
        cases.append((key, value))
    return cases


@pytest.mark.skipif(not _JDK_ON_PATH, reason="no JDK on PATH")
def test_writes_read_back_through_java_properties_load(tmp_path: Path) -> None:
    # Every write, through the reader the Minecraft server itself uses: key and
    # value must come back UTF-16 unit for UTF-16 unit.
    cases = _reference_cases()
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    expected: dict[str, str] = {}
    for index, (key, value) in enumerate(cases):
        name = f"{index:06d}.properties"
        (cases_dir / name).write_bytes(apply_overrides(b"", {key: value}))
        expected[name] = f"{_utf16_units(key)} {_utf16_units(value)}"
    probe = tmp_path / "Probe.java"
    probe.write_text(_PROBE_JAVA, encoding="ascii")

    result = subprocess.run(
        ["java", str(probe), str(cases_dir)],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr

    actual: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, _, dump = line.partition(" ")
        actual[name] = dump
    mismatches = [
        (name, expected[name], actual.get(name))
        for name in expected
        if actual.get(name) != expected[name]
    ]
    assert not mismatches, (
        f"{len(mismatches)} of {len(cases)} writes did not read back as written: "
        f"{mismatches[:3]}"
    )


# --- one parse per write (issue #2863) ----------------------------------------


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Record the content of every :func:`_parse` call and return the record."""

    parsed: list[bytes] = []
    real_parse = server_properties._parse

    def counting_parse(content: bytes) -> list[server_properties._Property]:
        parsed.append(content)
        return real_parse(content)

    monkeypatch.setattr(server_properties, "_parse", counting_parse)
    return parsed


def test_apply_platform_properties_parses_the_content_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Chaining set_server_port / set_rcon_properties / set_resource_pack_properties
    # re-read the whole file once per key written -- eight to ten byte-by-byte
    # passes for one call, and this one runs on the event loop (issue #2863).
    parsed = _count_parses(monkeypatch)
    content = b"motd=hi\n"
    apply_platform_properties(
        content,
        game_port=25565,
        rcon_password="tok",
        resource_pack=ResourcePackProperties(
            url=_RP_URL, sha1=_RP_SHA1, require=True, prompt="Use it"
        ),
    )
    assert parsed == [content]


def test_remove_keys_parses_the_content_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One pass per key removed, so the cost grew with the size of the key set.
    parsed = _count_parses(monkeypatch)
    content = b"a=1\nb=2\nc=3\nd=4\ne=5\n"
    assert remove_keys(content, {"a", "c", "e"}) == b"b=2\nd=4\n"
    assert parsed == [content]


def test_clear_resource_pack_properties_parses_the_content_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fixed four passes for the four pack keys, on the import/restore path.
    parsed = _count_parses(monkeypatch)
    content = b"resource-pack=u\nresource-pack-sha1=s\nmotd=hi\n"
    assert clear_resource_pack_properties(content) == b"motd=hi\n"
    assert parsed == [content]


# --- the batched writes against the per-key ones they replace (issue #2863) ----

# The write paths were batched for speed alone, so the bar is byte identity with
# the per-key chain, not "equivalent enough" -- and the chain is kept below, in
# terms of the single-key helpers it was written in, so the two can be run
# against each other over inputs picked to break them. The fragments are the
# corners this file's format has: every Java separator, degenerate and escaped
# spellings of the platform's own keys, duplicates, comments, continuations,
# each line terminator including a lone CR, non-UTF-8 bytes, and files that end
# mid-line. Concatenating them is itself adversarial -- a fragment with no
# terminator splices into the next one's key.
_DIFFERENTIAL_FRAGMENTS: list[bytes] = [
    b"",
    b"\n",
    b"   \n",
    b"#comment\n",
    b"!bang comment\n",
    b"#comment\\\n",
    b"motd=hi\n",
    b"motd=hi",
    b"motd=trailing ",
    b"nokeyandnonewline",
    b"=empty key\n",
    b"orphan\n",
    b"server-port=25565\n",
    b"server-port:25565\n",
    b"server-port 25565\n",
    b"server-port = 25565 \n",
    b"server-port=1\nserver-port=2\n",
    b"#server-port=11111\n",
    b"server-port\n",
    b"server-port \n",
    b" server-port=leading\n",
    b"server-port\\ =degenerate\n",
    b"\\u0073erver-port=escaped\n",
    b"enable-rcon=false\n",
    b"enable-rcon=\n",
    b"rcon.port=1\n",
    b"rcon\\.port=degenerate\n",
    b"rcon.password=known\n",
    b"rcon.password:known\n",
    b"rcon.password=\n",
    b"rcon.password= \n",
    b"rcon.password=a\nrcon.password=\n",
    b"\\u0072con.password=escaped\n",
    b"resource-pack=old\n",
    b"resource-pack=old",
    b"rcon.password=known",
    b"resource-pack:old\n",
    b"resource-pack=a\nresource-pack=b\n",
    b"resource-pack=old\\\n  continued\n",
    b"resource-pack-sha1=deadbeef\n",
    b"require-resource-pack=true\n",
    b"resource-pack-prompt=hi\n",
    b"resource-pack-prompt=\\u65E5\\u672C\n",
    b"require-\\\n",
    b"require\n",
    b"motd=\xff\xfe raw latin-1 \xe9\n",
    b"\xff\xfe=\xe9\n",
    b"crlf=1\r\n",
    b"cronly=1\r",
    b"even=1\\\\\n",
    b"odd=1\\\n",
    b"\\\n",
    b"resource-pack=old\\\n",
    b"rcon.password=\\\n",
    b"tail\\",
    b"tail\\\r\n",
    b"tail\\\r",
]

# One assignment of each shape apply_platform_properties branches on, plus one
# whose values are the injection and encoding corners #2819 / #2820 name.
_DIFFERENTIAL_PACKS: list[ResourcePackProperties | None] = [
    None,
    ResourcePackProperties(url=_RP_URL, sha1=_RP_SHA1, require=True, prompt="Use it"),
    ResourcePackProperties(url=_RP_URL, sha1=_RP_SHA1, require=False, prompt=None),
    ResourcePackProperties(
        url="=lead\nrcon.password=evil", sha1="", require=True, prompt="\u65e5\\"
    ),
]

_DIFFERENTIAL_KEY_SETS: list[set[str]] = [
    set(),
    {"motd"},
    {"server-port"},
    {"resource-pack", "resource-pack-sha1"},
    set(PLATFORM_MANAGED_KEYS),
    {"motd", "server-port ", " server-port", "rcon.password", "", "orphan", "tail"},
]


def _chained_remove_keys(content: bytes, keys: set[str]) -> bytes:
    """Remove *keys* the way ``remove_keys`` did: one full parse per key.

    Sorted rather than in the set's own order, so agreeing with this also says
    the batched removal does not depend on the order the keys arrive in.
    """

    for key in sorted(keys):
        content = server_properties._clear_property(content, key)
    return server_properties._normalize(content)


def _chained_clear_resource_pack_properties(content: bytes) -> bytes:
    """Clear the four pack keys the way ``clear_resource_pack_properties`` did."""

    for key in (
        "resource-pack",
        "resource-pack-sha1",
        "require-resource-pack",
        "resource-pack-prompt",
    ):
        content = server_properties._clear_property(content, key)
    return server_properties._normalize(content)


def _chained_apply_platform_properties(
    content: bytes,
    *,
    game_port: int | None,
    rcon_password: str,
    resource_pack: ResourcePackProperties | None,
) -> bytes:
    """Apply the platform's keys by chaining the public helpers, as #2621 did."""

    if game_port is not None:
        content = set_server_port(content, game_port)
    content = set_rcon_properties(content, password=rcon_password)
    if resource_pack is None:
        return _chained_clear_resource_pack_properties(content)
    content = set_resource_pack_properties(
        content,
        url=resource_pack.url,
        sha1=resource_pack.sha1,
        require=resource_pack.require,
        prompt=resource_pack.prompt,
    )
    if resource_pack.prompt is None:
        content = _chained_remove_keys(content, {"resource-pack-prompt"})
    return content


def _paired_files() -> list[bytes]:
    """Return every ordered pair of fragments, each before and after every other."""

    return [a + b for a in _DIFFERENTIAL_FRAGMENTS for b in _DIFFERENTIAL_FRAGMENTS]


def _drawn_files() -> list[bytes]:
    """Return files of up to six drawn fragments, so keys repeat across spellings.

    Seeded, so a mismatch is reproducible from the test name alone.
    """

    rng = random.Random(2863)
    return [
        b"".join(rng.choice(_DIFFERENTIAL_FRAGMENTS) for _ in range(rng.randint(3, 6)))
        for _ in range(500)
    ]


def _differential_files() -> list[bytes]:
    """Return every file the two forms are run over."""

    return _DIFFERENTIAL_FRAGMENTS + _paired_files() + _drawn_files()


def _apply_cases() -> list[tuple[bytes, int | None, ResourcePackProperties | None]]:
    """Return the (file, port, assignment) triples the two apply forms are run over.

    Each fragment and each drawn file meets every assignment shape. The 3025
    ordered pairs meet the two that CLEAR -- an unassigned pack and one with no
    prompt -- because a removal is what an append can interact with, and running
    all four over them costs seconds for no further reach.
    """

    ports: tuple[int | None, ...] = (None, 25565)
    cases = [
        (content, port, pack)
        for content in _DIFFERENTIAL_FRAGMENTS + _drawn_files()
        for port in ports
        for pack in _DIFFERENTIAL_PACKS
    ]
    cases += [
        (content, port, pack)
        for content in _paired_files()
        for port in ports
        for pack in (_DIFFERENTIAL_PACKS[0], _DIFFERENTIAL_PACKS[2])
    ]
    return cases


def test_remove_keys_matches_removing_one_key_at_a_time() -> None:
    mismatches = [
        (content, sorted(keys))
        for content in _differential_files()
        for keys in _DIFFERENTIAL_KEY_SETS
        if remove_keys(content, keys) != _chained_remove_keys(content, keys)
    ]
    assert not mismatches, f"{len(mismatches)} differ, first: {mismatches[:3]}"


def test_clear_resource_pack_properties_matches_clearing_one_key_at_a_time() -> None:
    mismatches = [
        content
        for content in _differential_files()
        if clear_resource_pack_properties(content)
        != _chained_clear_resource_pack_properties(content)
    ]
    assert not mismatches, f"{len(mismatches)} differ, first: {mismatches[:3]}"


def test_apply_platform_properties_matches_the_per_key_chain() -> None:
    mismatches = [
        (content, game_port, pack)
        for content, game_port, pack in _apply_cases()
        if apply_platform_properties(
            content, game_port=game_port, rcon_password="tok", resource_pack=pack
        )
        != _chained_apply_platform_properties(
            content, game_port=game_port, rcon_password="tok", resource_pack=pack
        )
    ]
    assert not mismatches, f"{len(mismatches)} differ, first: {mismatches[:3]}"


def test_apply_platform_properties_keeps_the_chain_on_a_continued_last_line() -> None:
    # The one file shape the batch cannot reproduce, so the chain still writes it
    # (issue #2863). Appending after a last line that ends in an odd backslash run
    # merges the two: "require-\" + the appended "resource-pack=..." is read as
    # ONE require-resource-pack line, which the chain's next write then replaces
    # in place -- taking the resource-pack line it had just written with it. The
    # result is a file missing a key the platform owns; preserving that is not an
    # endorsement of it, it is this change staying a speed change.
    content = b"enable-rcon=x\nrcon.port=y\nrcon.password=z\nrequire-\\\n"
    out = apply_platform_properties(
        content,
        game_port=None,
        rcon_password="tok",
        resource_pack=ResourcePackProperties(
            url=_RP_URL, sha1=_RP_SHA1, require=True, prompt=None
        ),
    )
    assert out == (
        b"enable-rcon=true\n"
        b"rcon.port=25575\n"
        b"rcon.password=z\n"
        b"require-resource-pack=true\n" + f"resource-pack-sha1={_RP_SHA1}\n".encode()
    )
    assert _raw_values(out, "resource-pack") == []
