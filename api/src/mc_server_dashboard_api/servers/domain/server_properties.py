"""Minimal ``server.properties`` line rewrites (issues #311, #335).

The at-rest ``server.properties`` must sometimes be rewritten so what the server
binds matches what the platform tracks: the ``server-port`` line is kept in sync
with the DB ``game_port`` (#311), and the RCON keys are enforced so the console /
graceful-stop path works out of the box (#335). These are pure,
standard-library-only helpers that do the line edits, preserving every other line
and its order, or appending a key when the file has no such line. A wholly absent
file (a legacy server with no seeded properties, #243) is the caller's decision,
not this module's: the restore, config-overrides and resource-pack assign paths
pass an empty body to :func:`apply_platform_properties`, so the file they seed
carries the whole platform half, while unassigning a pack skips the write
altogether rather than publishing a file that holds nothing (#2621, #2810).

Mojang's ``server.properties`` is a Java ``.properties`` file, so every helper here
READS it through :func:`_parse`, a ``java.util.Properties.load``-compatible logical
line reader: ``key=value``, ``key:value`` and ``key value`` are one key, a line
ending in an odd backslash run continues onto the next, ``#`` and ``!`` start
comments, escapes (``rcon\\.password``, ``\\uXXXX``) resolve in keys and values, and
a repeated key takes its LAST occurrence. Matching only ``key=`` used to make every
platform-managed key bypassable by respelling it -- the guard saw no change while
the server read the respelled line (issue #2811). The Worker parses the same file
with the same rules (``worker/internal/javaproperties``), and the two test tables
(``PARITY_CASES`` / ``parityCases``) are mirrored to keep them from drifting.

WRITES stay canonical: :func:`_set_property` always emits ``key=value``, never a
respelling -- but it does emit ``java.util.Properties.store``'s escapes, so what
a caller submits is what ``Properties.load`` reads back. Unescaped, a newline in
an override value ended the line and turned the rest of the value into further
property lines, planted below the platform's own and therefore the ones Java
reads (issue #2819). Everything outside printable ASCII is spelled as the
``\\uXXXX`` escape ``Properties.store`` emits, so a written line is pure ASCII
however the file is later decoded: a Japanese ``resource-pack-prompt`` reaches
the server as itself instead of as the mojibake UTF-8 bytes made of it, and the
webui's UTF-8 file editor reads it back as itself too (issue #2820). Untouched
bytes are spliced through verbatim, so a file the platform did not write to is
preserved exactly.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

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

# The number of random bytes behind the per-server RCON password (issue #335). The
# password lives only in server.properties (the worker reads it there); it is never
# persisted in the DB. ``secrets.token_urlsafe`` returns ~1.3 chars per byte.
_RCON_PASSWORD_BYTES = 32


def new_rcon_password() -> str:
    """Generate a fresh per-server RCON secret (the default token generator)."""

    return secrets.token_urlsafe(_RCON_PASSWORD_BYTES)


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

# The four keys one resource-pack assignment owns: the set
# :func:`clear_resource_pack_properties` removes when a server has no pack, named
# once so the removal and the re-apply path cannot drift apart (#1177, #1253).
_RESOURCE_PACK_KEYS: frozenset[str] = frozenset(
    {
        _RESOURCE_PACK_KEY,
        _RESOURCE_PACK_SHA1_KEY,
        _REQUIRE_RESOURCE_PACK_KEY,
        _RESOURCE_PACK_PROMPT_KEY,
    }
)


@dataclass(frozen=True)
class _Property:
    """One logical property line, as ``java.util.Properties.load`` sees it.

    ``start`` and ``end`` are byte offsets into the parsed content: the first byte
    of the natural line the property begins on, and the first byte past the
    terminator that ends it. A backslash continuation spans several natural lines
    yet is ONE property, so dropping ``content[start:end]`` drops all of them --
    leaving a tail behind would keep the value alive AND corrupt the next line.
    """

    key: str
    value: str
    start: int
    end: int


# The characters ``Properties.load`` counts as blanks: no CR/LF (those terminate a
# line) and no Unicode whitespace (it reads latin-1 bytes, not text).
_BLANKS = b" \t\f"

_HEX_DIGITS = "0123456789abcdefABCDEF"

# The escapes ``java.util.Properties.store`` emits for a character that would
# otherwise be read as structure rather than text. A newline is the dangerous one
# (it ends the line, so the rest of a value becomes property lines of its own,
# issue #2819) and a trailing backslash the subtle one (an odd run continues the
# logical line onto the next, swallowing it).
_ESCAPES = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\f": "\\f"}

# The characters that carry meaning only in a LEADING position within a value: a
# blank there is padding the reader skips, and ``=`` / ``:`` / ``#`` / ``!`` read
# as the separator or as a comment marker. Inside a key each of them still ends
# it, so a key escapes them everywhere -- as ``Properties.store`` does.
_SPECIALS = frozenset("=:#! ")


def _natural_line(content: bytes, offset: int) -> tuple[bytes, int]:
    """Return the line at *offset* without its terminator, and the next offset.

    ``\\r\\n``, a lone ``\\r`` and a lone ``\\n`` all terminate a line, as they do
    for ``Properties.load``; an unterminated final line runs to the end. Splitting
    on ``\\n`` alone would fold a CR-separated pair into one line and hide the
    second half of it from every caller here.
    """

    end = offset
    while end < len(content) and content[end] not in b"\n\r":
        end += 1
    if end >= len(content):
        return content[offset:end], end
    if content[end : end + 2] == b"\r\n":
        return content[offset:end], end + 2
    return content[offset:end], end + 1


def _ends_with_odd_backslash(line: bytes) -> bool:
    """True when *line* ends in an odd backslash run, so it continues onto the next.

    An even run is a sequence of escaped backslashes and ends the logical line.
    """

    return (len(line) - len(line.rstrip(b"\\"))) % 2 == 1


def _load_convert(raw: bytes) -> str:
    """Decode *raw* as latin-1 and resolve the ``.properties`` escapes.

    latin-1 is what ``Properties.load(InputStream)`` uses, and it is a
    byte-preserving bijection: a ``server.properties`` that is not valid UTF-8 (a
    latin-1 ``motd`` is ordinary, #2623) decodes without raising, and two DIFFERENT
    byte sequences never collapse onto one value the way a lossy decode would --
    which is what keeps the comparison guard honest.

    A malformed ``\\uXXXX`` -- which the reference implementation rejects with an
    exception -- yields the literal ``u`` and whatever followed it, so the guard
    reads a hand-mangled file rather than turning an ordinary write into a 500.
    Such a file makes the Java server refuse to load it at all, so there is no
    "right" value to agree on anyway.
    """

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
            digits = text[i : i + 4]
            if len(digits) == 4 and all(d in _HEX_DIGITS for d in digits):
                out.append(chr(int(digits, 16)))
                i += 4
            else:
                out.append("u")
        else:
            out.append({"t": "\t", "r": "\r", "n": "\n", "f": "\f"}.get(char, char))
    return "".join(out)


def _split_key_value(line: bytes) -> tuple[str, str]:
    """Split one logical line (already left-trimmed) into its decoded key and value.

    The key runs to the first UNESCAPED ``=``, ``:`` or blank. Blanks after it, then
    one optional ``=`` / ``:``, then further blanks are skipped; everything left is
    the value, TRAILING whitespace included -- Java keeps it, so ``25565 `` is not
    ``25565`` and the guard must not pretend otherwise.
    """

    key_end = value_start = len(line)
    has_sep = False
    backslash = False
    for i in range(len(line)):
        char = line[i : i + 1]
        if not backslash and char in b"=:":
            key_end, value_start, has_sep = i, i + 1, True
            break
        if not backslash and char in _BLANKS:
            key_end, value_start = i, i + 1
            break
        backslash = char == b"\\" and not backslash
    while value_start < len(line):
        char = line[value_start : value_start + 1]
        if char not in _BLANKS:
            if has_sep or char not in b"=:":
                break
            has_sep = True
        value_start += 1
    return _load_convert(line[:key_end]), _load_convert(line[value_start:])


def _parse(content: bytes) -> list[_Property]:
    """Return every property in *content*, in file order, parsed the Java way.

    Blank lines and ``#`` / ``!`` comments are dropped; a comment does NOT continue
    on a trailing backslash, while an ordinary line does and its continuation is
    never itself a comment. Repeated keys are all returned -- callers decide
    between "the value the server reads" (the last) and "every occurrence" (the
    guard, which must see an appended duplicate as the change it is).
    """

    props: list[_Property] = []
    offset = 0
    while offset < len(content):
        start = offset
        line, offset = _natural_line(content, offset)
        line = line.lstrip(_BLANKS)
        if not line or line[:1] in (b"#", b"!"):
            continue
        while _ends_with_odd_backslash(line):
            line = line[:-1]
            if offset >= len(content):
                break
            continuation, offset = _natural_line(content, offset)
            line += continuation.lstrip(_BLANKS)
        key, value = _split_key_value(line)
        props.append(_Property(key=key, value=value, start=start, end=offset))
    return props


def _normalize(content: bytes) -> bytes:
    """Return *content* ending in the single trailing newline Mojang's files carry."""

    return content if content.endswith(b"\n") else content + b"\n"


def _raw_values(content: bytes, key: str) -> list[str]:
    """Return every value *content* gives *key*, in file order.

    Every occurrence matters, not just the last: Java's ``Properties.load`` is
    last-occurrence-wins, so an appended second line for a key is what the server
    actually reads -- and comparing the whole list is what makes leaving the
    original line intact and appending a respelled one show up as a change.
    """

    return [prop.value for prop in _parse(content) if prop.key == key]


def _get_property(content: bytes, key: str) -> str | None:
    """Return the value the Java server reads for *key*, or ``None`` if absent."""

    values = _raw_values(content, key)
    return values[-1] if values else None


def _clear_property(content: bytes, key: str) -> bytes:
    """Return *content* with EVERY property line for *key* removed.

    Every one, not just the first: a second line for the same key is what Java
    reads, so a clear that left it behind would leave the pack the archive named
    live for the server (issues #2621, #2811). Bytes outside the removed ranges are
    spliced through untouched, so unrelated lines keep their exact encoding.
    """

    out = bytearray()
    cursor = 0
    for prop in _parse(content):
        if prop.key != key:
            continue
        out += content[cursor : prop.start]
        cursor = prop.end
    out += content[cursor:]
    return bytes(out)


def _escape_char(char: str) -> str:
    """Return the spelling *char* is written as, inside a key or inside a value.

    A structural character takes its :data:`_ESCAPES` spelling; anything outside
    printable ASCII becomes the Unicode escape ``Properties.store`` emits for it,
    one per UTF-16 unit, so a code point above the BMP is written as the
    surrogate pair a Java string holds it as. The backslashes emitted here are
    output, never input to another pass, so a literal backslash in the text is
    still the one thing that doubles.

    The threshold is ``Properties.store(OutputStream)``'s own -- escape
    everything outside ``0x20``-``0x7E`` -- and NOT "whatever latin-1 cannot
    hold", because Java is not the file's only reader. A raw ``0xE9`` byte is the
    right ``e``-acute to a latin-1 reader alone: the webui reads the file through
    a UTF-8 decoder, which turns that byte into U+FFFD, so saving the text back
    would report the platform's own key as changed and refuse the write. The
    escape is the right spelling to EVERY reader, and costs nothing -- printable
    ASCII writes are unchanged and Java reads both spellings identically.
    """

    if char in _ESCAPES:
        return _ESCAPES[char]
    if 0x20 <= ord(char) <= 0x7E:
        return char
    units = char.encode("utf-16-be", "surrogatepass").hex().upper()
    return "".join("\\u" + units[i : i + 4] for i in range(0, len(units), 4))


def _escape_key(key: str) -> str:
    """Return *key* spelled so ``Properties.load`` reads it back verbatim.

    Every :data:`_SPECIALS` character is escaped wherever it sits, because each
    would otherwise end the key -- or, first on the line, start a comment.
    """

    return "".join(
        "\\" + char if char in _SPECIALS else _escape_char(char) for char in key
    )


def _escape_value(value: str) -> str:
    """Return *value* spelled so ``Properties.load`` reads it back verbatim.

    :func:`_escape_char` applies to every character, while a :data:`_SPECIALS`
    character only needs escaping as the FIRST character -- past the separator
    they are ordinary text, so a ``motd`` or a resource-pack URL keeps the
    readable spelling an operator expects to find in the file.
    """

    escaped = "".join(_escape_char(char) for char in value)
    return "\\" + escaped if value[:1] in _SPECIALS else escaped


def _set_property(content: bytes, key: str, value: str) -> bytes:
    """Return *content* with *key* set to *value*, canonically and exactly once.

    The first property line for *key* is replaced in place by ``key=value``, in the
    canonical spelling whatever spelling it had; every later line for the key is
    removed, because Java would read that one instead. When the file has no such
    line, ``key=value`` is appended. Other lines and their order are preserved.

    Key and value are escaped on the way out (:func:`_escape_key`,
    :func:`_escape_value`), so caller-supplied text is written as ONE property
    line saying exactly what was submitted -- the write side of the same Java
    rules :func:`_parse` reads with, and what keeps an override value from
    injecting further lines (issue #2819).

    The escaped line is pure ASCII, because :func:`_escape_char` has already
    spelled everything outside ``0x20``-``0x7E`` as an escape -- so the latin-1
    encode (the encoding ``Properties.load(InputStream)`` reads the file in) is
    total, and every reader of the file agrees on what was submitted whichever
    encoding it assumes (issue #2820).
    """

    new_line = f"{_escape_key(key)}={_escape_value(value)}".encode("latin-1")
    matches = [prop for prop in _parse(content) if prop.key == key]
    if not matches:
        if content and not content.endswith(b"\n"):
            content += b"\n"
        return content + new_line + b"\n"
    out = bytearray()
    cursor = 0
    for index, prop in enumerate(matches):
        out += content[cursor : prop.start]
        if index == 0:
            out += new_line + b"\n"
        cursor = prop.end
    out += content[cursor:]
    return _normalize(bytes(out))


def _ends_in_dangling_continuation(content: bytes, props: list[_Property]) -> bool:
    """True when *content*'s last logical line continues onto whatever follows it.

    A line ending in an odd backslash run CONTINUES onto the next one. At EOF
    there is no next line, so the value simply ends -- until something is
    appended, which that continuation then swallows, merging the two into one
    property spelled as neither the file nor the write said. The appended key is
    then absent from the file the server reads, which is a setting the caller
    explicitly asked for silently dropped (issue #2994), so :func:`_rewrite` ends
    the continuation before it appends.

    Only the LAST logical line can dangle -- an earlier one would have consumed
    the line below it -- and it dangles only when its last natural line carries
    the odd run. That line belongs to a property only when the last property's
    span reaches EOF: a comment does not continue, so a file ending in ``#c\\``
    swallows nothing (:func:`_parse`).

    That is :func:`_parse`'s reading, the one this module's own reads of the file
    go through. Java's can end sooner: while a continued line is still empty,
    Java reads a ``#`` / ``!`` line after it as a comment rather than as its
    continuation, so to Java a file ending ``\\``, ``#c\\`` ends in a comment.
    The append needs a line in front of it all the same -- without one,
    :func:`_parse` joins the appended key onto ``#c`` -- and
    :func:`_continuation_end` picks one that leaves Java's reading alone too.

    Neither this shape nor the file that has one is normal for a
    ``server.properties``; a hand-edited or truncated file is where it turns up.
    """

    if not props or props[-1].end != len(content):
        return False
    tail = content
    if tail.endswith(b"\r\n"):
        tail = tail[:-2]
    elif tail.endswith((b"\n", b"\r")):
        tail = tail[:-1]
    cut = max(tail.rfind(b"\n"), tail.rfind(b"\r"))
    return _ends_with_odd_backslash(tail[cut + 1 :].lstrip(_BLANKS))


def _continuation_end(content: bytes, start: int) -> bytes:
    """Return the line that ends *content*'s dangling last property, at *start*.

    The line has to end the continuation without changing what
    ``Properties.load`` reads from the property it ends (issue #2994). An empty
    line does that whenever the logical line has something in it: its value ran
    to EOF, and an empty continuation is the same empty tail.

    Not when the logical line is EMPTY once its continuation is accumulated --
    every natural line of it is a lone backslash, blanks aside. Java reads
    ``"" = ""`` from that line only because EOF ends it; followed by an empty
    line, it reads the line as blank and the property is gone. ``=`` ends it as
    ``"" = ""`` instead. Unless *content* ends in ``\\r\\n``: Java already reads
    no property at all from an empty continued line that ends the file that way,
    so ``=`` would invent one, and the empty line is right after all. Both are
    what the JDK reads (checked in the tests); :func:`_parse` reads a ``""``
    property from the CRLF file as well, which is its divergence from Java
    rather than a property to preserve.

    That logical line need not begin at *start*. While a continued line is still
    empty, Java reads a ``#`` / ``!`` line after it as a comment, which never
    continues, and starts a new logical line below it; :func:`_parse` joins the
    comment on instead (:func:`_ends_in_dangling_continuation`). So the walk
    skips such a comment as Java does and goes on with a new, empty line.
    *start* itself is a line Java starts too: :func:`_parse` only ever joins
    lines Java keeps apart, never the reverse. A blank line needs no rule of its
    own -- it ends the line either way, and :func:`_parse` never joins past one.

    The ``=`` is for the empty line only: after ``k=v`` it would make the value
    ``v=``. When the comment is the last line, nothing dangles for Java at all,
    and the empty line is only what keeps :func:`_parse` from joining the append
    onto it.
    """

    continued = False
    offset = start
    while offset < len(content):
        line, offset = _natural_line(content, offset)
        line = line.lstrip(_BLANKS)
        if line[:1] in (b"#", b"!"):
            continued = False
        elif line == b"\\":
            continued = True
        else:
            return b"\n"
    if continued and not content.endswith(b"\r\n"):
        return b"=\n"
    return b"\n"


def _rewrite(
    content: bytes,
    props: list[_Property],
    values: Mapping[str, str],
    cleared: AbstractSet[str],
) -> bytes:
    """Return *content* with each *values* key set and each *cleared* key removed.

    The batched form of :func:`_set_property` and :func:`_clear_property`: one
    walk of *props* -- the caller's own :func:`_parse` of *content*, so a caller
    that already holds one does not pay for a second -- instead of a fresh full
    parse per key. Chaining the single-key helpers cost one byte-by-byte pass
    each, seven to nine of them for one :func:`apply_platform_properties` call
    (issue #2863).

    Per key the outcome is the single-key helpers' own: the first property line
    for a *values* key is replaced in place by the canonical ``key=value`` and
    every later one is dropped, a key the file has no line for is appended, and a
    *cleared* key loses every line it has. *values* is applied in ITERATION
    ORDER, which is the order its appended lines land in -- the order the chained
    helpers appended them in. The result is NOT normalized; callers do that, as
    they did around the single-key helpers.

    An append is preceded by whatever it takes to make it its own logical line:
    the newline the content does not already end in, and -- when what is left
    after the removals still ends in a dangling continuation
    (:func:`_ends_in_dangling_continuation`) -- a line that ends that
    continuation and leaves it reading as it did (:func:`_continuation_end`), so
    the appended key is not swallowed into the line above (issue #2994).

    The removals are what decide whether the tail still dangles. Only a tail
    spliced through verbatim can -- once *cursor* reaches the end of *content*,
    the last property was either replaced by a canonical ``key=value`` line or
    removed along with the whole dangling line, and what an earlier property
    leaves behind ends its own logical line by construction.
    """

    lines = {
        key: f"{_escape_key(key)}={_escape_value(value)}".encode("latin-1")
        for key, value in values.items()
    }
    written: set[str] = set()
    out = bytearray()
    cursor = 0
    for prop in props:
        if prop.key not in lines and prop.key not in cleared:
            continue
        out += content[cursor : prop.start]
        cursor = prop.end
        if prop.key in lines and prop.key not in written:
            out += lines[prop.key] + b"\n"
            written.add(prop.key)
    out += content[cursor:]
    appended = [line for key, line in lines.items() if key not in written]
    if appended:
        if out and not out.endswith(b"\n"):
            out += b"\n"
        if cursor < len(content) and _ends_in_dangling_continuation(content, props):
            out += _continuation_end(content, props[-1].start)
        for line in appended:
            out += line + b"\n"
    return bytes(out)


def set_server_port(content: bytes, port: int) -> bytes:
    """Return ``content`` with its ``server-port`` line set to ``port``.

    Rewrites the first ``server-port`` property line in place and drops any later
    one; if the file has none, appends ``server-port=<port>``. Other lines and
    their order are preserved. An empty ``content`` yields a file with just the
    port line. The result always ends with a single trailing newline (Mojang's
    convention).

    The rewritten line is normalized to ``\n`` regardless of the file's existing
    line endings, so a CRLF file gains mixed endings on that one line. This is
    harmless: ``server.properties`` is parsed line-by-line and trailing ``\r`` is
    stripped as whitespace.
    """

    return _set_property(content, _PORT_KEY, str(port))


def set_rcon_properties(content: bytes, *, password: str) -> bytes:
    """Return ``content`` with RCON enabled and its port/password enforced (#335).

    ``enable-rcon=true`` and ``rcon.port=<RCON_PORT>`` are always set (rewritten in
    place or appended), so a fresh or imported ``server.properties`` with RCON off
    or a stray port is corrected. ``rcon.password`` is set to ``password`` only when
    the file has no live password line or its value is empty: a non-empty existing
    password is preserved, so an importer's known credential keeps working -- in
    whatever spelling the file used, since "what the file already says" is read
    with the same Java rules the worker reads it with. Other lines and their order
    are preserved; the result ends with a single trailing newline.
    """

    content = _set_property(content, _ENABLE_RCON_KEY, "true")
    content = _set_property(content, _RCON_PORT_KEY, str(RCON_PORT))
    if not _get_property(content, _RCON_PASSWORD_KEY):
        content = _set_property(content, _RCON_PASSWORD_KEY, password)
    return content


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

    content = _set_property(content, _RESOURCE_PACK_KEY, url)
    content = _set_property(content, _RESOURCE_PACK_SHA1_KEY, sha1)
    content = _set_property(
        content, _REQUIRE_RESOURCE_PACK_KEY, "true" if require else "false"
    )
    if prompt is not None:
        content = _set_property(content, _RESOURCE_PACK_PROMPT_KEY, prompt)
    return content


def apply_overrides(content: bytes, overrides: dict[str, str]) -> bytes:
    """Return ``content`` with each ``key=value`` pair in *overrides* applied.

    Each key is set via the same rewrite-or-append logic as the other helpers: the
    first property line for the key is rewritten in place (any later one for the
    same key is dropped); if none exists, ``key=value`` is appended. Other lines
    and their order are preserved; the result ends with a single trailing newline
    (issue #1209).

    These pairs are the one caller-supplied input this module writes, so the
    escaping :func:`_set_property` applies is what makes each override exactly one
    property line: a newline, a leading blank or a leading ``=``/``:`` in a value
    is written escaped, and reads back as the text that was submitted rather than
    as further lines the platform-managed-key guard never saw (issue #2819).
    """

    for key, value in overrides.items():
        content = _set_property(content, key, value)
    return _normalize(content)


def remove_keys(content: bytes, keys: AbstractSet[str]) -> bytes:
    """Return ``content`` with every line matching a key in *keys* removed.

    Every property line for each key is deleted entirely, in whatever spelling it
    used and including the continuation lines it spans. Other lines and their
    order are preserved; the result ends with a single trailing newline (#1242).

    One parse whatever the size of *keys* (issue #2863), where clearing a key at
    a time re-read the whole file per key. Order never mattered here and still
    does not: a removed property line is spliced out whole, so what is left
    parses exactly as it did, and *keys* arrives as a set anyway.
    """

    return _normalize(_rewrite(content, _parse(content), {}, keys))


def _platform_managed_values(content: bytes) -> dict[str, list[str]]:
    """Return the values *content* gives each :data:`PLATFORM_MANAGED_KEYS` key.

    Same lists :func:`_raw_values` returns per key, collected in ONE
    :func:`_parse` pass instead of one pass per key -- sixteen walks over the two
    files for a single comparison, each of them byte by byte (issue #2831). A key
    the file never mentions is absent from the mapping rather than mapped to an
    empty list, which is why the caller reads it with a default.
    """

    values: dict[str, list[str]] = {}
    for prop in _parse(content):
        if prop.key in PLATFORM_MANAGED_KEYS:
            values.setdefault(prop.key, []).append(prop.value)
    return values


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

    "Live" is decided by :func:`_parse`, i.e. exactly as ``Properties.load`` would:
    ``rcon.password:evil`` appended below an untouched ``rcon.password=...``, a
    whitespace separator, an escaped or ``\\uXXXX``-spelled key, a value continued
    over a backslash -- each is the change the server would read, so each is
    reported. Respelling a line without changing the value it parses to is not a
    change, because the server reads the same thing either way (issue #2811).

    Never decodes as UTF-8: a ``server.properties`` is not required to be valid
    UTF-8 (a latin-1 ``motd`` is ordinary), and a guard that raised on one would
    turn an otherwise-fine write into a 500. The latin-1 decode behind the parse is
    byte-preserving, so two DIFFERENT invalid sequences stay different values.

    *current* must be the copy the write actually lands on -- the authoritative
    Storage copy at rest, the worker's live working set while running. Those two
    diverge in these very keys (Minecraft's boot rewrite fills in defaults the
    seeded file omits), so comparing against the wrong one refuses honest edits.
    """

    current_values = _platform_managed_values(current)
    incoming_values = _platform_managed_values(incoming)
    return sorted(
        key
        for key in PLATFORM_MANAGED_KEYS
        if current_values.get(key, []) != incoming_values.get(key, [])
    )


@dataclass(frozen=True)
class ResourcePackProperties:
    """The DB-owned resource-pack values for a server (issue #2621).

    Built from the server's assignment row (and the pack it points at) by the
    caller; ``None`` in place of one of these means the server has no pack
    assigned, so the keys are cleared instead.
    """

    url: str
    sha1: str
    require: bool
    prompt: str | None


def apply_platform_properties(
    content: bytes,
    *,
    game_port: int | None,
    rcon_password: str,
    resource_pack: ResourcePackProperties | None,
) -> bytes:
    """Return ``content`` with EVERY :data:`PLATFORM_MANAGED_KEYS` key re-applied.

    The one place that turns "what the DB says about this server" into the
    platform's half of a ``server.properties``. Import and restore both republish
    a file that came from somewhere else -- an export archive, a backup taken
    before a re-port -- so without this the archive's ``server-port`` becomes the
    server's real bind port while the DB keeps the port the rest of the system
    trusts, and hydrate copies the disagreement forever (issue #2621).

    Per key:

    - ``server-port`` becomes ``game_port``. A ``None`` ``game_port`` (a legacy row
      that predates port tracking, DEPLOYMENT.md Section 7) owns nothing, so the
      file's own value is left alone rather than replaced by a guess.
    - The RCON triple goes through :func:`set_rcon_properties`: ``enable-rcon`` and
      ``rcon.port`` are enforced, while a non-empty ``rcon.password`` already in
      *content* is preserved -- the file is that credential's only source of truth
      (#335), so a republished file that carries a working one keeps it, and
      ``rcon_password`` only fills in a missing or empty one.
    - The resource-pack keys come from the assignment: ``None`` clears all four
      (the server has no pack), and a ``prompt`` of ``None`` removes just the
      prompt key, since "no prompt" is what the assignment row then says.

    Those decisions are read off ONE :func:`_parse` and applied in ONE pass
    (issue #2863). Chaining the public helpers cost a full byte-by-byte parse per
    key written -- seven to nine of them per call -- and this call, unlike the
    comparison guard, is not behind ``asyncio.to_thread`` at every call site, so
    on an oversized root file (a restore or an import, or one predating the #2809
    cap) it occupied the event loop. The helpers themselves are unchanged and
    have callers of their own.

    The one pass is also what keeps every key: chaining them re-parsed between
    writes, so when the file's last logical line continued onto an appended one,
    the next helper saw the merged key and rewrote or cleared the span it now
    covered -- taking the line it had just written with it, and leaving the file
    without a setting the caller had asked for (issue #2994). Nothing here reads
    back what it wrote, and the append ends such a continuation before it writes
    (:func:`_rewrite`).
    """

    props = _parse(content)
    values: dict[str, str] = {}
    if game_port is not None:
        values[_PORT_KEY] = str(game_port)
    values[_ENABLE_RCON_KEY] = "true"
    values[_RCON_PORT_KEY] = str(RCON_PORT)
    passwords = [prop.value for prop in props if prop.key == _RCON_PASSWORD_KEY]
    if not (passwords and passwords[-1]):
        values[_RCON_PASSWORD_KEY] = rcon_password
    cleared: AbstractSet[str] = frozenset()
    if resource_pack is None:
        cleared = _RESOURCE_PACK_KEYS
    else:
        values[_RESOURCE_PACK_KEY] = resource_pack.url
        values[_RESOURCE_PACK_SHA1_KEY] = resource_pack.sha1
        values[_REQUIRE_RESOURCE_PACK_KEY] = (
            "true" if resource_pack.require else "false"
        )
        if resource_pack.prompt is None:
            cleared = frozenset({_RESOURCE_PACK_PROMPT_KEY})
        else:
            values[_RESOURCE_PACK_PROMPT_KEY] = resource_pack.prompt
    return _normalize(_rewrite(content, props, values, cleared))


def clear_resource_pack_properties(content: bytes) -> bytes:
    """Return ``content`` with the 4 resource pack keys removed (issue #1177).

    Removes ``resource-pack``, ``resource-pack-sha1``, ``require-resource-pack``,
    and ``resource-pack-prompt`` entirely -- every occurrence of each, in whatever
    spelling, so an untrusted archive's ``resource-pack:http://...`` cannot survive
    the clear that import and restore run (issues #2621, #2811). Other lines and
    their order are preserved; the result ends with a single trailing newline.

    One parse, where clearing the four keys one at a time was a fixed four passes
    over the whole file (issue #2863).
    """

    return _normalize(_rewrite(content, _parse(content), {}, _RESOURCE_PACK_KEYS))
