"""The rule for the filename of an uploaded file (issues #3907, #3922).

Each multipart upload that keeps the part's ``filename`` asks the same question
of it: the resource-pack upload, the plugin upload and the file upload. The
parser decodes the name with whatever ``charset`` the request declares, so a
client can put in it any character, a lone surrogate included.
"""

from __future__ import annotations

import unicodedata

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
    DEL and C1), which would split a log line or drive the terminal showing it,
    and no lone surrogate, which has no UTF-8 encoding at all. It must not be blank,
    and must fit :data:`MAX_FILENAME_BYTES`. Any other character is a legitimate
    name, Unicode spaces and joiners included.

    Only an upload asks: a resource pack stored before the check keeps its name,
    and every read path serves it as before.
    """

    return (
        filename.strip() != ""
        and not any(
            c in "/\\" or unicodedata.category(c) in ("Cc", "Cs") for c in filename
        )
        and len(filename.encode("utf-8")) <= MAX_FILENAME_BYTES
    )
