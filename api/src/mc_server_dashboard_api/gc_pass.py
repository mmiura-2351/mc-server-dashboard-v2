"""How a storage GC pass ends and where the next one starts (issues #3906, #3934).

The JAR-pool GC (versions) and the plugin-cache and resource-pack GCs (servers)
skip an object whose delete failed and go on (issue #3871). A store that fails
every delete would then cost each orphan its retries and a WARNING with a
traceback, so each pass ends after :data:`MAX_CONSECUTIVE_FAILED_DELETES` failed
deletes in a row, and its summary line names :data:`STOPPED_AFTER_CONSECUTIVE_FAILURES`
as the reason. A successful delete starts the count again.

A delete the store gave no answer to ends the pass at once instead. When that
failure is in fact tied to one object, every pass would end at it and never reach
the objects after it, so each GC keeps a :class:`ResumePoint` and its next pass
starts after that object.

It is a neutral root-level module that imports only the standard library, so both
contexts' inward layers may import it. That is the ``rfc3339`` precedent in the
root-level import contract.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

MAX_CONSECUTIVE_FAILED_DELETES = 5

STOPPED_AFTER_CONSECUTIVE_FAILURES = "stopped_after_consecutive_failures"


class ResumePoint:
    """Where a GC's next pass starts (issue #3934).

    ``key`` is the listing key of the object whose delete ended the last pass on
    a whole-store failure, or ``None``. The GC sets it there, and clears it when a
    pass walks the whole listing without one. It lives in process memory only:
    each GC is wired with one instance, which every use case object that runs that
    GC shares (the JAR-pool loop and its manual trigger), and an API restart
    starts it empty.
    """

    def __init__(self) -> None:
        self.key: str | None = None

    def order[T](self, entries: Iterable[T], key_of: Callable[[T], str]) -> list[T]:
        """``entries`` in key order, starting after :attr:`key` and wrapping round.

        The object at :attr:`key` comes last, after every other, so one that keeps
        failing cannot starve the rest. Sorting, not the listing's own order,
        makes "after" mean the same on every pass: the fs pool lists in directory
        order.
        """

        after = self.key
        return sorted(
            entries,
            key=lambda entry: (
                after is not None and key_of(entry) <= after,
                key_of(entry),
            ),
        )
