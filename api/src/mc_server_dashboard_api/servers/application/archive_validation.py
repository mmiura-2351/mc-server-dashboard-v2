"""The pool the upload doors validate archives on, and its admission (issue #3843).

Two doors take an uploaded archive of up to 512 MiB and decompress it to judge it
before anything is stored or written: the backup upload
(:class:`~.backups.UploadBackup`) and the files upload with ``extract``
(:class:`~.files.UploadFile`). That work is CPU-bound, and the caps do not bound all
of it: a crafted archive costs minutes near the body cap (issue #3835). On
asyncio's shared default executor, a few such uploads took every worker, and so
starved everything else that runs there: every fs Storage call, the storage
sweep, import and resource packs.

So both doors validate on :data:`ARCHIVE_VALIDATION_WORKERS` threads of their own,
as the password KDF does on its own pool (issue #1696), and nothing else runs
there. Both doors admit at most :data:`ADMITTED_ARCHIVE_UPLOADS` archive uploads
at a time between them through one :class:`ArchiveUploadAdmission`: as many as
the pool validates at once, and as many again waiting for it. Each admitted
upload holds its body in memory, so that also bounds the memory the doors hold
at once. Both sizes are constants, as the KDF pool's and the data-plane
transfer limit's are.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TypeVar

from mc_server_dashboard_api.servers.domain.errors import UploadBusyError

ARCHIVE_VALIDATION_WORKERS = 2

ADMITTED_ARCHIVE_UPLOADS = 2 * ARCHIVE_VALIDATION_WORKERS

_ARCHIVE_VALIDATION_EXECUTOR = ThreadPoolExecutor(
    max_workers=ARCHIVE_VALIDATION_WORKERS, thread_name_prefix="archive-validation"
)

_T = TypeVar("_T")


async def run_archive_validation(
    func: Callable[..., _T], /, *args: object, **kwargs: object
) -> _T:
    """Run a blocking archive read on the archive-validation pool.

    A cancelled call stops waiting at once. A call still queued then never runs;
    one already running cannot be stopped, and runs on to its own end.
    """

    return await asyncio.get_running_loop().run_in_executor(
        _ARCHIVE_VALIDATION_EXECUTOR, functools.partial(func, *args, **kwargs)
    )


class ArchiveUploadAdmission:
    """The archive uploads both doors have admitted, at most ``limit`` at once.

    An upload past the limit is refused at once with :class:`UploadBusyError`
    rather than queued, so it holds no memory while it waits. One instance serves
    the process (``dependencies.get_archive_upload_admission``).
    """

    def __init__(self, limit: int = ADMITTED_ARCHIVE_UPLOADS) -> None:
        self._free = limit

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one slot for the block, or raise :class:`UploadBusyError`.

        The slot frees when the block ends, however it ends. A request cancelled
        while its validation runs therefore frees its slot while that thread
        runs on; the pool still bounds those threads.
        """

        if self._free == 0:
            raise UploadBusyError()
        self._free -= 1
        try:
            yield
        finally:
            self._free += 1
