"""Domain errors for the versions context.

Pure exceptions, standard-library only. The edge maps them to HTTP codes; the
download/verify path maps a hash mismatch to a clean start failure.
"""

from __future__ import annotations


class VersionError(Exception):
    """Base class for versions-domain errors."""


class UnknownServerTypeError(VersionError):
    """A requested server type is not catalogued at M1 (e.g. ``forge``)."""


class UnknownVersionError(VersionError):
    """A requested (server_type, version) pair is not offered by the catalog."""


class CatalogUnavailableError(VersionError):
    """The external source could not be reached and no cached payload exists.

    Raised after the bounded retry budget is spent with no last-good cache to fall
    back on (FR-VER-2). The edge maps it to a transient 503
    ``catalog_unavailable``.

    Not to be confused with the *content* catalog's
    :class:`mc_server_dashboard_api.servers.domain.errors.CatalogUpstreamFailedError`,
    which is the gateway case (502 ``catalog_upstream_failed``) and carries the
    rationale for the split (issue #2406).
    """


class JarHashMismatchError(VersionError):
    """A downloaded JAR's digest did not match the catalog's expected hash.

    The bytes are rejected and never stored; the start that triggered the download
    fails cleanly before placement/dispatch (the issue's ensure-on-start ruling).
    """


class JarDownloadError(VersionError):
    """A JAR download failed at the transport edge (non-2xx or transport error).

    The start that triggered the download fails cleanly before placement; the edge
    maps it to a transient 503 (``jar_unavailable``).
    """


class JarTooLargeError(VersionError):
    """A JAR download exceeded the maximum allowed size and was aborted.

    The body is streamed and aborted once it crosses the cap (no unbounded buffer
    before verification), so a runaway or hostile response cannot exhaust memory.
    The edge maps it to the same ``jar_unavailable`` 503 surface.
    """


class StoreNotOwnedError(VersionError):
    """The JAR-pool GC refused: its store belongs to another database (#3870).

    The pool is diffed against this database's server rows, so on a store another
    database also uses every JAR of that database would look unreferenced. The
    message carries the owner marker's verdict. The edge maps it to 409
    ``store_not_owned``.
    """


class JarPoolUnreachableError(VersionError):
    """The JAR pool's store gave no answer at all (#3871).

    Raised by ``JarPool.delete`` when the storage adapter reports the store itself
    unreachable rather than a failure of the one JAR: a transport failure on the
    object backend, a gone device or remote-fs mount on fs. The JAR-pool GC ends
    its pass on it, since every later delete would fail the same way, each after the
    full timeout; any other delete failure only skips that JAR.
    """
