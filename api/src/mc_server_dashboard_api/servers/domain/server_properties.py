"""Minimal ``server.properties`` ``server-port`` rewrite (issue #311).

When the game port is changed via the API, the at-rest ``server.properties`` must
be rewritten so the real bind port and the tracked ``game_port`` stay in sync.
This is the pure, standard-library-only helper that does the line edit: it
rewrites the existing ``server-port=<port>`` line in place (preserving every other
line and ordering), or appends one when the file has no such line. A wholly absent
file (a legacy server with no seeded properties, #243) is handled by the caller,
which passes an empty body so this produces a file with just the port line.

Mojang's ``server.properties`` is a Java ``.properties`` file; for the single key
we touch, ``key=value`` line matching on a comment-aware, whitespace-trimmed key
is sufficient (we never need to parse values or escapes).
"""

from __future__ import annotations

_PORT_KEY = "server-port"


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

    text = content.decode()
    new_line = f"{_PORT_KEY}={port}"

    # Split into content lines, dropping a single trailing empty element from a
    # trailing newline so an append lands on its own line. An empty input becomes
    # no lines, so the result is just the port line.
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
            and stripped.split("=", 1)[0].strip() == _PORT_KEY
        ):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)

    if not replaced:
        out.append(new_line)

    # Always end with a single trailing newline (Mojang's convention and the
    # create-seed format ``server-port=<port>\n``).
    return ("\n".join(out) + "\n").encode()
