"""When a storage GC pass gives up on its failing deletes (issue #3906).

The JAR-pool GC (versions) and the plugin-cache and resource-pack GCs (servers)
skip an object whose delete failed and go on (issue #3871). A store that fails
every delete would then cost each orphan its retries and a WARNING with a
traceback, so each pass ends after :data:`MAX_CONSECUTIVE_FAILED_DELETES` failed
deletes in a row, and its summary line names :data:`STOPPED_AFTER_CONSECUTIVE_FAILURES`
as the reason. A successful delete starts the count again.

It is a neutral root-level module that imports nothing, so both contexts' inward
layers may import it. That is the ``rfc3339`` precedent in the root-level import
contract.
"""

from __future__ import annotations

MAX_CONSECUTIVE_FAILED_DELETES = 5

STOPPED_AFTER_CONSECUTIVE_FAILURES = "stopped_after_consecutive_failures"


class ResumePoint:
    """Where a GC's next pass starts (issue #3934).

    ``key`` is the listing key of the object whose delete ended the last pass on
    a whole-store failure, or ``None``. It lives in process memory only: each GC
    is wired with one instance, which every use case object that runs that GC
    shares (the JAR-pool loop and its manual trigger), and an API restart starts
    it empty.
    """

    def __init__(self) -> None:
        self.key: str | None = None
