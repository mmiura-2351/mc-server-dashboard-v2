"""How a storage GC pass ends and where the next one starts (issues #3906, #3934).

The JAR-pool GC (versions) and the plugin-cache and resource-pack GCs (servers)
skip an object whose delete failed and go on (issue #3871). A store that fails
every delete would then cost each orphan its retries and a WARNING with a
traceback, so each pass ends after :data:`MAX_CONSECUTIVE_FAILED_DELETES` failed
deletes in a row, and its summary line names :data:`STOPPED_AFTER_CONSECUTIVE_FAILURES`
as the reason. A successful delete starts the count again.

A delete the store gave no answer to ends the pass at once instead. When that
failure, or the run of five, is in fact tied to the objects it was for, every pass
would end there and never reach the objects after them. Each GC therefore keeps a
:class:`ResumePoint`, and its next pass starts after the object at which the last
one ended early.

It is a neutral root-level module that imports only the standard library, so both
contexts' inward layers may import it. That is the ``rfc3339`` precedent in the
root-level import contract.
"""

from __future__ import annotations

import bisect
import random
from collections.abc import Callable, Iterable

MAX_CONSECUTIVE_FAILED_DELETES = 5

STOPPED_AFTER_CONSECUTIVE_FAILURES = "stopped_after_consecutive_failures"


def _coin_flip() -> bool:
    return random.random() < 0.5


class ResumePoint:
    """Where a GC's next pass starts (issue #3934).

    ``key`` is the listing key of the object whose failed delete ended the last
    pass early, or ``None``. That is a delete the store gave no answer to, or the
    fifth failed delete in a row. Passes that end early therefore form one
    continuous scan round the keys. The GC clears the key when a pass walks the
    whole listing without ending early.

    It lives in process memory only: each GC is wired with one instance, which
    every use case object that runs that GC shares (the JAR-pool loop and its
    manual trigger), and an API restart starts it empty. A pass with no key starts
    at a place in key order that ``start`` picks, given the number of keys: at
    random by default, so a process that ends before its next pass, or restarts,
    does not start every pass at the same key.
    """

    def __init__(
        self,
        start: Callable[[int], int] = random.randrange,
        descending: Callable[[], bool] = _coin_flip,
    ) -> None:
        self.key: str | None = None
        self._start = start
        self._descending = descending

    def order[T](self, entries: Iterable[T], key_of: Callable[[T], str]) -> list[T]:
        """``entries`` in key order, starting after :attr:`key` and wrapping round.

        The object at :attr:`key` comes last, after every other, so one that keeps
        failing cannot starve the rest. With no :attr:`key`, the walk starts where
        ``start`` picks. Sorting, not the listing's own order, makes "after" mean
        the same on every pass: the fs pool lists in directory order.
        """

        walk = sorted(entries, key=key_of)
        if not walk:
            return walk
        if self.key is None:
            first = self._start(len(walk))
        else:
            first = bisect.bisect_right(walk, self.key, key=key_of)
        return walk[first:] + walk[:first]
