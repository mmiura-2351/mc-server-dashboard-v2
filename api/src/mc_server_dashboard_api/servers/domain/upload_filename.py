"""The rule for the filename of an uploaded file (issues #3907, #3922).

Each multipart upload that keeps the part's ``filename`` asks the same question
of it: the resource-pack upload, the plugin upload and the file upload. The
parser decodes the name with whatever ``charset`` the request declares, so a
client can put in it any character, a lone surrogate included.
"""

from __future__ import annotations

from mc_server_dashboard_api.name_characters import is_refused_name_character

# The longest name an upload may carry, in UTF-8 bytes. A plugin or a file is
# written under this name on the server's filesystem, where ``NAME_MAX`` is 255.
# A resource pack's name is the last segment of its object key, after the
# 52-byte ``resource-packs/<pack-id>/``, so the key stays at 307 bytes at most,
# well under the 1024 an S3 key may hold.
MAX_FILENAME_BYTES = 255


def is_valid_upload_filename(filename: str) -> bool:
    """Whether ``filename`` can name an uploaded file.

    The name becomes one segment of a path or of an object key, and of a public
    download URL for a resource pack, and logs name it. So it must be one segment,
    with no ``/`` or ``\\``. It must carry no control character (Unicode ``Cc``: C0,
    DEL and C1) and no line or paragraph separator (``Zl``, ``Zp``), which would
    split a log line or drive the terminal showing it (issue #3988), no lone
    surrogate, which has no UTF-8 encoding at all, and no noncharacter:
    U+FFFE and U+FFFF, which XML 1.0 cannot carry while the object store lists its
    keys in XML, and the other 64 (U+FDD0 to U+FDEF, and the last two code points
    of every other plane), which Unicode reserves for a program's internal use and
    XML discourages. It must not be blank, and must fit :data:`MAX_FILENAME_BYTES`.
    Any other character is a legitimate name: Unicode spaces, joiners and bidi
    controls, and a code point unassigned today, which may name something
    tomorrow.

    Only an upload asks: a resource pack stored before the check keeps its name,
    and every read path serves it as before.
    """

    return (
        filename.strip() != ""
        and not any(is_refused_filename_character(c) for c in filename)
        and len(filename.encode("utf-8")) <= MAX_FILENAME_BYTES
    )


def is_refused_filename_character(c: str) -> bool:
    """Whether no uploaded file's name may carry ``c``.

    ``/``, ``\\``, a control character, a line or paragraph separator, a lone
    surrogate or a noncharacter; see :func:`is_valid_upload_filename` for why.
    """

    return c in "/\\" or is_refused_name_character(c) or _is_noncharacter(c)


def _is_noncharacter(c: str) -> bool:
    """Whether ``c`` is one of the 66 code points Unicode reserves as noncharacters."""

    code = ord(c)
    return 0xFDD0 <= code <= 0xFDEF or code & 0xFFFE == 0xFFFE
