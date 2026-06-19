"""Stream a set of mod jars into a single zip with bounded memory (issue #1265).

The client modpack download bundles a server's client-needed jars into one zip.
Jars can be large (up to the 256 MiB upload cap each), so the archive is built
incrementally: each jar is pulled from the :class:`ModStore` in chunks, written
through :meth:`zipfile.ZipFile.open` (streaming write mode), and the growing zip
buffer is drained and yielded after every chunk. At no point is a whole jar -- or
the whole archive -- held in memory at once.

Two client mods can share a ``filename`` (the basename of distinct library
entries). Identical entry names would silently corrupt the archive, so colliding
names are de-duplicated by inserting a counter before the extension
(``sodium.jar`` -> ``sodium (1).jar``).
"""

from __future__ import annotations

import io
import os
import zipfile
from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.mod import Mod
from mc_server_dashboard_api.servers.domain.mod_store import ModStore

# Yield buffered zip bytes once this many have accumulated (back-pressure unit).
_DRAIN_THRESHOLD = 1024 * 1024


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
    its (collision-deduplicated) ``filename``. The zip buffer is flushed after
    every chunk so peak memory stays near one chunk regardless of jar size.
    """

    buf = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for mod in mods:
            entry_name = _unique_name(mod.filename, used_names)
            with zf.open(entry_name, "w") as entry:
                async for chunk in store.open(mod.id, mod.filename):
                    entry.write(chunk)
                    if buf.tell() >= _DRAIN_THRESHOLD:
                        yield buf.getvalue()
                        buf.seek(0)
                        buf.truncate(0)
    # Flush the central directory written by ZipFile.__exit__.
    remaining = buf.getvalue()
    if remaining:
        yield remaining
