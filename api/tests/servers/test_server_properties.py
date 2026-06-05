"""Tests for the ``server.properties`` server-port rewrite (issue #311)."""

from __future__ import annotations

from mc_server_dashboard_api.servers.domain.server_properties import (
    RCON_PORT,
    set_rcon_properties,
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


def test_replaces_only_the_first_server_port_line() -> None:
    content = b"server-port=1\nserver-port=2\n"
    assert set_server_port(content, 9) == b"server-port=9\nserver-port=2\n"


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
