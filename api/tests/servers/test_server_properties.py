"""Tests for the ``server.properties`` server-port rewrite (issue #311)."""

from __future__ import annotations

from mc_server_dashboard_api.servers.domain.server_properties import set_server_port


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


def test_replaces_only_the_first_server_port_line() -> None:
    content = b"server-port=1\nserver-port=2\n"
    assert set_server_port(content, 9) == b"server-port=9\nserver-port=2\n"


def test_no_trailing_newline_input_appends_without_adding_blank_line() -> None:
    content = b"motd=hi"
    # The file had no trailing newline; the append still lands on its own line and
    # a trailing newline is added (matching the seeded-file convention).
    assert set_server_port(content, 25570) == b"motd=hi\nserver-port=25570\n"
