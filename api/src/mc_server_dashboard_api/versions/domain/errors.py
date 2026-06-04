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
    back on (FR-VER-2). The edge maps it to a transient 503.
    """


class JarHashMismatchError(VersionError):
    """A downloaded JAR's digest did not match the catalog's expected hash.

    The bytes are rejected and never stored; the start that triggered the download
    fails cleanly before placement/dispatch (the issue's ensure-on-start ruling).
    """
