"""Minimal ``server.properties`` line rewrites (issues #311, #335).

The at-rest ``server.properties`` must sometimes be rewritten so what the server
binds matches what the platform tracks: the ``server-port`` line is kept in sync
with the DB ``game_port`` (#311), and the RCON keys are enforced so the console /
graceful-stop path works out of the box (#335). These are pure,
standard-library-only helpers that do the line edits, preserving every other line
and its order, or appending a key when the file has no such line. A wholly absent
file (a legacy server with no seeded properties, #243) is handled by the caller,
which passes an empty body so the helper produces a file with just its keys.

Mojang's ``server.properties`` is a Java ``.properties`` file; for the few keys we
touch, ``key=value`` line matching on a comment-aware, whitespace-trimmed key is
sufficient (we never need to parse values or escapes).
"""

from __future__ import annotations

_PORT_KEY = "server-port"
_ENABLE_RCON_KEY = "enable-rcon"
_RCON_PORT_KEY = "rcon.port"
_RCON_PASSWORD_KEY = "rcon.password"
_RESOURCE_PACK_KEY = "resource-pack"
_RESOURCE_PACK_SHA1_KEY = "resource-pack-sha1"
_REQUIRE_RESOURCE_PACK_KEY = "require-resource-pack"
_RESOURCE_PACK_PROMPT_KEY = "resource-pack-prompt"

# The in-container RCON port the worker connects to (issue #335). It is never
# published to the host (the container driver drops the host RCON publication,
# #218), so a fixed value is fine across servers.
RCON_PORT = 25575


def _split_content_lines(content: bytes) -> list[str]:
    """Decode ``content`` into property lines, dropping the trailing-newline empty.

    An empty input becomes no lines, so callers that only append produce a file
    with just their appended lines.
    """

    lines = content.decode().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _is_key_line(line: str, key: str) -> bool:
    """True when ``line`` is the live (non-comment) ``key=...`` property line."""

    stripped = line.lstrip()
    return (
        not stripped.startswith("#")
        and "=" in stripped
        and stripped.split("=", 1)[0].strip() == key
    )


def _get_property(lines: list[str], key: str) -> str | None:
    """Return the value of the first live ``key=...`` line, or ``None`` if absent."""

    for line in lines:
        if _is_key_line(line, key):
            return line.split("=", 1)[1]
    return None


def _clear_property(lines: list[str], key: str) -> list[str]:
    """Remove the first live ``key=...`` line entirely, if present."""

    return [line for line in lines if not _is_key_line(line, key)]


def _set_property(lines: list[str], key: str, value: str) -> list[str]:
    """Set ``key`` to ``value`` in ``lines``, rewriting in place or appending.

    Rewrites the first live (non-comment) ``key=...`` line; if none exists,
    appends ``key=value``. Other lines and their order are preserved.
    """

    new_line = f"{key}={value}"
    replaced = False
    out: list[str] = []
    for line in lines:
        if not replaced and _is_key_line(line, key):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new_line)
    return out


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

    lines = _set_property(_split_content_lines(content), _PORT_KEY, str(port))
    # Always end with a single trailing newline (Mojang's convention and the
    # create-seed format ``server-port=<port>\n``).
    return ("\n".join(lines) + "\n").encode()


def set_rcon_properties(content: bytes, *, password: str) -> bytes:
    """Return ``content`` with RCON enabled and its port/password enforced (#335).

    ``enable-rcon=true`` and ``rcon.port=<RCON_PORT>`` are always set (rewritten in
    place or appended), so a fresh or imported ``server.properties`` with RCON off
    or a stray port is corrected. ``rcon.password`` is set to ``password`` only when
    the file has no live password line or its value is empty: a non-empty existing
    password is preserved, so an importer's known credential keeps working. Other
    lines and their order are preserved; the result ends with a single trailing
    newline.
    """

    lines = _split_content_lines(content)
    lines = _set_property(lines, _ENABLE_RCON_KEY, "true")
    lines = _set_property(lines, _RCON_PORT_KEY, str(RCON_PORT))
    existing = _get_property(lines, _RCON_PASSWORD_KEY)
    if not existing:
        lines = _set_property(lines, _RCON_PASSWORD_KEY, password)
    return ("\n".join(lines) + "\n").encode()


def set_resource_pack_properties(
    content: bytes,
    *,
    url: str,
    sha1: str,
    require: bool = False,
    prompt: str | None = None,
) -> bytes:
    """Return ``content`` with the resource pack keys set (issue #1177).

    ``resource-pack``, ``resource-pack-sha1``, and ``require-resource-pack`` are
    always set. ``resource-pack-prompt`` is set only when ``prompt`` is not None;
    otherwise the existing value (if any) is left untouched.
    """

    lines = _split_content_lines(content)
    lines = _set_property(lines, _RESOURCE_PACK_KEY, url)
    lines = _set_property(lines, _RESOURCE_PACK_SHA1_KEY, sha1)
    lines = _set_property(
        lines, _REQUIRE_RESOURCE_PACK_KEY, "true" if require else "false"
    )
    if prompt is not None:
        lines = _set_property(lines, _RESOURCE_PACK_PROMPT_KEY, prompt)
    return ("\n".join(lines) + "\n").encode()


def clear_resource_pack_properties(content: bytes) -> bytes:
    """Return ``content`` with the 4 resource pack keys removed (issue #1177).

    Removes ``resource-pack``, ``resource-pack-sha1``, ``require-resource-pack``,
    and ``resource-pack-prompt`` entirely. Other lines and their order are
    preserved; the result ends with a single trailing newline.
    """

    lines = _split_content_lines(content)
    lines = _clear_property(lines, _RESOURCE_PACK_KEY)
    lines = _clear_property(lines, _RESOURCE_PACK_SHA1_KEY)
    lines = _clear_property(lines, _REQUIRE_RESOURCE_PACK_KEY)
    lines = _clear_property(lines, _RESOURCE_PACK_PROMPT_KEY)
    return ("\n".join(lines) + "\n").encode()
