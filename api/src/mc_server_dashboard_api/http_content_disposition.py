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

    ascii_fallback = "".join(
        c if (0x20 <= ord(c) < 0x7F and c not in '"\\') else "_" for c in filename
    )
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"
