"""Count a zip's entries before ``zipfile`` builds them (issue #3936).

Opening an archive with :class:`zipfile.ZipFile` builds a ``ZipInfo`` for every
record in its central directory, about 2.7 µs each, roughly 15 s for a crafted
512 MiB archive. An entry cap checked on ``infolist()`` fires
only after all of that, so the jar and resource-pack readers check theirs with
:func:`has_more_entries_than` before they open the archive.

``ZipFile`` takes the directory's size and location from the end-of-central-
directory record and parses every record in those bytes. It never reads the
entry count the record declares, so a crafted archive can declare one entry and
hold millions. So the declared count is checked first, and then the records are
walked as ``ZipFile`` walks them, reading only each one's three lengths, about
an eighth of the cost, and the walk stops one record past the cap.
"""

from __future__ import annotations

import io
import struct
import zipfile

# A central-directory record: its signature, and the lengths of the name, the
# extra field and the comment that follow its 46 fixed bytes.
_RECORD = struct.Struct("<4s24xHHH")
_RECORD_BYTES = 46
_RECORD_SIGNATURE = b"PK\x01\x02"

# The fields of the end record zipfile's own reader returns (its
# ``_ECD_ENTRIES_TOTAL``, ``_ECD_SIZE`` and ``_ECD_LOCATION``).
_ENTRIES_TOTAL = 4
_DIRECTORY_SIZE = 5
_RECORD_LOCATION = 9


def has_more_entries_than(content: bytes, cap: int) -> bool:
    """Whether ``ZipFile`` would build more than ``cap`` entries for ``content``.

    ``content`` is an archive :func:`zipfile.is_zipfile` accepts. Its end record
    is read with zipfile's own reader, so the directory walked is the one
    ``ZipFile`` parses: a zip64 record overrides the classic one, and data
    prepended to the archive shifts it. That reader is private and untyped;
    ``pyproject.toml`` pins Python 3.13. A directory that is malformed where the
    walk stops is refused by ``ZipFile`` at the same record.
    """

    end = zipfile._EndRecData(io.BytesIO(content))  # type: ignore[attr-defined]
    if end[_ENTRIES_TOTAL] > cap:
        return True
    size: int = end[_DIRECTORY_SIZE]
    start = end[_RECORD_LOCATION] - size
    if start < 0:
        return False
    directory = memoryview(content)[start : start + size]
    walked = 0
    entries = 0
    while walked < size:
        if walked + _RECORD_BYTES > len(directory):
            return False
        signature, name, extra, comment = _RECORD.unpack_from(directory, walked)
        if signature != _RECORD_SIGNATURE:
            return False
        entries += 1
        if entries > cap:
            return True
        walked += _RECORD_BYTES + name + extra + comment
    return False
