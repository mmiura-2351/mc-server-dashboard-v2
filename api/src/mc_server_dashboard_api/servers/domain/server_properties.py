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

from collections.abc import Set as AbstractSet

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

# The ``server.properties`` keys the platform owns: their values come from the
# DB row or from a platform decision, never from the user (issue #2623). Defined
# here, once, so every write path to the file consults the same set instead of
# growing its own list -- the configuration path (``manage_server``), the files
# path (``application/files``), and the import/restore path (``export_import``).
#
# - ``server-port`` tracks the DB ``game_port`` (#311, #243).
# - The RCON triple is the credential the worker reaches the server with (#335);
#   ``rcon.password`` lives ONLY in this file, so the file is its sole source of
#   truth.
# - The resource-pack keys are written from the assignment row by
#   :func:`set_resource_pack_properties` / :func:`clear_resource_pack_properties`
#   (#1177, #1253).
PLATFORM_MANAGED_KEYS: frozenset[str] = frozenset(
    {
        _PORT_KEY,
        _ENABLE_RCON_KEY,
        _RCON_PORT_KEY,
        _RCON_PASSWORD_KEY,
        _RESOURCE_PACK_KEY,
        _RESOURCE_PACK_SHA1_KEY,
        _REQUIRE_RESOURCE_PACK_KEY,
        _RESOURCE_PACK_PROMPT_KEY,
    }
)


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


def _raw_values(content: bytes, key: str) -> list[bytes]:
    """Return every live ``key=...`` value in *content*, in file order, as bytes.

    Byte-level on purpose: this backs the comparison guard, which must not care
    whether the file is valid UTF-8. A strict decode would turn a latin-1 ``motd``
    into a 500, and a lossy one would collapse two DIFFERENT invalid sequences
    into the same replacement character -- a change slipping past the guard.

    Every occurrence matters, not just the first: Java's ``Properties.load`` is
    last-occurrence-wins, so an appended second line for a key is what the server
    actually reads. Values are trimmed so a reformatting edit (or a CRLF/LF
    round-trip through an editor) does not read as a value change.
    """

    wanted = key.encode()
    values: list[bytes] = []
    for line in content.split(b"\n"):
        stripped = line.lstrip()
        if stripped.startswith(b"#"):
            continue
        name, sep, value = stripped.partition(b"=")
        if sep and name.strip() == wanted:
            values.append(value.strip())
    return values


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


def apply_overrides(content: bytes, overrides: dict[str, str]) -> bytes:
    """Return ``content`` with each ``key=value`` pair in *overrides* applied.

    Each key is set via the same rewrite-or-append logic as the other helpers:
    the first live (non-comment) ``key=...`` line is rewritten in place; if none
    exists, ``key=value`` is appended. Other lines and their order are preserved;
    the result ends with a single trailing newline (issue #1209).
    """

    lines = _split_content_lines(content)
    for key, value in overrides.items():
        lines = _set_property(lines, key, value)
    return ("\n".join(lines) + "\n").encode()


def remove_keys(content: bytes, keys: AbstractSet[str]) -> bytes:
    """Return ``content`` with every line matching a key in *keys* removed.

    Each key's first live (non-comment) ``key=...`` line is deleted entirely.
    Other lines and their order are preserved; the result ends with a single
    trailing newline (issue #1242).
    """

    lines = _split_content_lines(content)
    for key in keys:
        lines = _clear_property(lines, key)
    return ("\n".join(lines) + "\n").encode()


def changed_platform_managed_keys(current: bytes, incoming: bytes) -> list[str]:
    """Return the platform-managed keys *incoming* changes relative to *current*.

    The comparison is against the file's own current bytes, not against the DB:
    ``rcon.password`` has no other source of truth (it is never persisted in the
    DB -- the worker reads it here), and comparing against the DB would refuse a
    faithful edit of an already-drifted file. So the question this answers is
    exactly "does this write CHANGE a key the platform owns?" (issue #2623).

    A key counts as changed when its live values differ in any way: an edited
    value, a removed or commented-out line, a line added where the file had none,
    or a second occurrence appended after an untouched first one. Returns the
    offending keys sorted, or an empty list when the write leaves them all alone.

    Compares bytes, never decoded text: a ``server.properties`` is not required to
    be valid UTF-8 (a latin-1 ``motd`` is ordinary), and a guard that raised on one
    would turn an otherwise-fine write into a 500.

    *current* must be the copy the write actually lands on -- the authoritative
    Storage copy at rest, the worker's live working set while running. Those two
    diverge in these very keys (Minecraft's boot rewrite fills in defaults the
    seeded file omits), so comparing against the wrong one refuses honest edits.
    """

    return sorted(
        key
        for key in PLATFORM_MANAGED_KEYS
        if _raw_values(current, key) != _raw_values(incoming, key)
    )


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
