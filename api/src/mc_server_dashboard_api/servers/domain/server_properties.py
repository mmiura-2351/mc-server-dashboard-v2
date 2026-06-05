"""Minimal ``server.properties`` key rewrites (issues #311, #335).

Two pure, standard-library-only edits to Mojang's ``server.properties``:

- :func:`set_server_port` keeps the real bind port and the tracked ``game_port``
  in sync when the game port is changed via the API (issue #311).
- :func:`apply_rcon_settings` enables RCON so the console / graceful-stop path
  works out of the box on a fresh or imported server (issue #335). Without this,
  Minecraft generates ``enable-rcon=false`` on first boot and ``/command`` is
  dead until a manual edit.

Both edit ``key=value`` lines in place, preserving every other line and ordering,
or appending the line when the file has no such key. A wholly absent file (a
legacy server with no seeded properties, #243) is handled by the caller, which
passes an empty body so the result is a file with just the edited keys.

Mojang's ``server.properties`` is a Java ``.properties`` file; for the few keys
we touch, ``key=value`` line matching on a comment-aware, whitespace-trimmed key
is sufficient (we never need to parse values or escapes).
"""

from __future__ import annotations

_PORT_KEY = "server-port"

# The RCON keys the worker reads from ``server.properties`` (the canonical source
# of a server's RCON settings — no DB column). ``rcon.port`` is the in-container
# port only; it is never published to the host (the container driver drops the
# host RCON publication, #218).
_RCON_ENABLE_KEY = "enable-rcon"
_RCON_PASSWORD_KEY = "rcon.password"
_RCON_PORT_KEY = "rcon.port"
_RCON_PORT_VALUE = "25575"


def set_server_port(content: bytes, port: int) -> bytes:
    """Return ``content`` with its ``server-port`` line set to ``port``.

    Rewrites the first non-comment ``server-port=...`` line in place; if none
    exists, appends ``server-port=<port>``. Other lines and their order are
    preserved. An empty ``content`` yields a file with just the port line. The
    result always ends with a single trailing newline (Mojang's convention).

    The rewritten line is normalized to ``\n`` regardless of the file's existing
    line endings, so a CRLF file gains mixed endings on that one line. This is
    harmless: ``server.properties`` is parsed line-by-line and trailing ``\r`` is
    stripped as whitespace.
    """

    return _set_key(content, _PORT_KEY, str(port))


def apply_rcon_settings(content: bytes, password: str) -> bytes:
    """Return ``content`` with RCON enabled (issue #335).

    Overwrites ``enable-rcon=true`` and ``rcon.port=25575`` (always — these are
    the platform's required values), and sets ``rcon.password=<password>`` only
    when the file does not already carry a non-empty password (so an imported
    archive's known password survives). Lines are rewritten in place or appended;
    all other lines and their order are preserved, and the result ends with a
    single trailing newline.
    """

    out = _set_key(content, _RCON_ENABLE_KEY, "true")
    out = _set_key(out, _RCON_PORT_KEY, _RCON_PORT_VALUE)
    if not _value_of(content, _RCON_PASSWORD_KEY):
        out = _set_key(out, _RCON_PASSWORD_KEY, password)
    return out


def _value_of(content: bytes, key: str) -> str | None:
    """Return the value of the first live ``key=...`` line, or ``None`` if absent.

    A commented line is not a live key. An empty value is returned as ``""`` (the
    key is present but blank), distinct from ``None`` (the key is absent).
    """

    for line in content.decode().split("\n"):
        stripped = line.lstrip()
        if (
            not stripped.startswith("#")
            and "=" in stripped
            and stripped.split("=", 1)[0].strip() == key
        ):
            return stripped.split("=", 1)[1].strip()
    return None


def _set_key(content: bytes, key: str, value: str) -> bytes:
    """Return ``content`` with the first live ``key=...`` line set to ``value``.

    Rewrites the first non-comment ``key=...`` line in place; if none exists,
    appends ``key=value``. Other lines and their order are preserved. An empty
    ``content`` yields a file with just the new line. The result always ends with
    a single trailing newline (Mojang's convention).
    """

    text = content.decode()
    new_line = f"{key}={value}"

    # Split into content lines, dropping a single trailing empty element from a
    # trailing newline so an append lands on its own line. An empty input becomes
    # no lines, so the result is just the new line.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if (
            not replaced
            and not stripped.startswith("#")
            and "=" in stripped
            and stripped.split("=", 1)[0].strip() == key
        ):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)

    if not replaced:
        out.append(new_line)

    # Always end with a single trailing newline (Mojang's convention and the
    # create-seed line format ``key=<value>\n``).
    return ("\n".join(out) + "\n").encode()
