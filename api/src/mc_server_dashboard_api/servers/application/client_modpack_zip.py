"""Stream a set of mod jars into a single zip with bounded memory (issue #1265).

The client modpack download bundles a server's client-needed jars into one zip.
Jars can be large (up to the 256 MiB upload cap each), so the archive is built
incrementally: each jar is pulled from the :class:`ModStore` in chunks and
written through :meth:`zipfile.ZipFile.open` (streaming write mode), and the zip
bytes produced are yielded as they accumulate. At no point is a whole jar -- or
the whole archive -- held in memory at once.

The archive is written into a **non-seekable** sink (:class:`_StreamSink`).
A seekable sink cannot be drained mid-write: ``ZipFile`` writes a placeholder
local header, then seeks backward to patch the CRC-32 and sizes when the entry
closes -- draining the buffer in between discards the bytes it later seeks into
and corrupts the archive. A non-seekable sink forces ``ZipFile`` onto the
data-descriptor path: CRC/size are emitted in a trailing record after each
entry's data and ``ZipFile`` never seeks back, so the sink can be drained safely.

Entries are stored uncompressed (:data:`zipfile.ZIP_STORED`): mod jars are
already-compressed zips, so deflating them again spends CPU for ~0% gain.

Two client mods can share a ``filename`` (the basename of distinct library
entries). Identical entry names would silently corrupt the archive, so colliding
names are de-duplicated by inserting a counter before the extension
(``sodium.jar`` -> ``sodium (1).jar``).
"""

from __future__ import annotations

import os
import zipfile
from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.mod import Mod
from mc_server_dashboard_api.servers.domain.mod_store import ModStore


class _StreamSink:
    """Non-seekable sink that buffers the zip bytes ``ZipFile`` writes.

    Reporting ``seekable() -> False`` (and providing no ``seek``) forces
    ``ZipFile`` onto the data-descriptor path, so it never seeks backward and the
    buffer can be drained between writes. ``tell`` tracks the running byte count,
    which ``ZipFile`` needs for central-directory offsets.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._position = 0

    def write(self, b: bytes, /) -> int:
        self._buffer += b
        self._position += len(b)
        return len(b)

    def tell(self) -> int:
        return self._position

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def seekable(self) -> bool:
        return False

    def drain(self) -> bytes:
        """Return the buffered bytes and clear the buffer."""

        chunk = bytes(self._buffer)
        self._buffer.clear()
        return chunk


def _unique_name(name: str, used: set[str]) -> str:
    """Return ``name`` if unused, else a counter-suffixed variant.

    ``sodium.jar`` -> ``sodium (1).jar`` -> ``sodium (2).jar`` ... The chosen
    name is recorded in ``used`` so the next caller sees it as taken.
    """

    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    counter = 1
    while True:
        candidate = f"{stem} ({counter}){ext}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        counter += 1


async def stream_client_modpack(
    store: ModStore, mods: list[Mod]
) -> AsyncIterator[bytes]:
    """Yield a zip archive of ``mods`` jars, drained as it is built.

    Each jar is read from ``store`` in chunks and written into the archive with
    its (collision-deduplicated) ``filename``. The sink is drained after every
    chunk so peak memory stays near one chunk regardless of jar size.
    """

    sink = _StreamSink()
    used_names: set[str] = set()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:
        for mod in mods:
            entry_name = _unique_name(mod.filename, used_names)
            with zf.open(entry_name, "w") as entry:
                async for chunk in store.open(mod.id, mod.filename):
                    entry.write(chunk)
                    yield sink.drain()
    # Flush the central directory written by ZipFile.__exit__.
    remaining = sink.drain()
    if remaining:
        yield remaining
