"""Build an attachment ``Content-Disposition`` header (RFC 6266 / RFC 5987).

Every download edge that names its payload needs the same header, built the same
hardened way, so the construction lives here once rather than once per route
module (issue #2357).

A filename reaching this function is attacker-influenced -- a server name, an
uploaded resource-pack name, a path segment inside the working set -- so it is
never interpolated raw:

* ``"`` and ``\\`` would break out of the quoted-string and let a crafted name
  inject extra header parameters;
* a control character (notably CR/LF) would split the header;
* a non-latin-1 character raises ``UnicodeEncodeError`` when Starlette encodes
  the header, 500-ing a legitimate Unicode name.

So two parameters are emitted: an ASCII-only ``filename`` fallback (anything
outside printable ASCII, plus quote and backslash, replaced by ``_``) for legacy
clients, and an RFC 5987 ``filename*`` carrying the UTF-8 percent-encoded name
for modern clients, which prefer it.
"""

from __future__ import annotations

from urllib.parse import quote


def content_disposition(filename: str) -> str:
    """Return an ``attachment`` disposition naming ``filename``, safely."""

    name = _basename(filename)
    ascii_fallback = "".join(
        c if (0x20 <= ord(c) < 0x7F and c not in '"\\') else "_" for c in name
    )
    encoded = quote(name, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def _basename(filename: str) -> str:
    """Drop any directory component the name carries (RFC 6266 Section 4.3).

    A name is free-form -- a server may be called ``../../etc/passwd`` -- and both
    parameters would otherwise carry the separators through: the ASCII fallback
    verbatim, and ``filename*`` percent-encoded but decoded straight back by the
    client. RFC 6266 tells recipients to ignore path information, but a sender
    must not emit it in the first place. Both separators are cut, because the
    saving client may be on Windows, where ``\\`` separates too. A name that is
    only separators, dots and whitespace leaves nothing usable, so it falls back
    to ``download``.
    """

    last = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return last if last.strip(".") else "download"
