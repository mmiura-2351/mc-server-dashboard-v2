"""The servers-side version-validation seam (the create path's view of the catalog).

Server create validates the requested ``(server_type, mc_version)`` against the
global version catalog — cheaply, with no download (the issue's ensure-on-start
ruling: create validates, start fetches). The servers domain/application may not
import the versions context (import-linter contract), so they depend on this
narrow Port; the wiring binds it to a versions-catalog-backed adapter.

``forge`` is the documented M1 non-goal: the DB CHECK enum still permits it, but
the catalog does not list/resolve it, so this seam rejects it as *unsupported* —
distinct from an unknown version. The two failure modes are separate exceptions so
the edge can map them to distinct, honest 422 reasons.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.errors import ServerError


class UnsupportedServerTypeError(ServerError):
    """The server type is valid in the schema but not resolvable at M1 (forge)."""


class UnknownVersionError(ServerError):
    """The requested MC version is not offered for the server type."""


class VersionValidator(abc.ABC):
    """Port: validate a ``(server_type, version)`` against the catalog (FR-VER-1)."""

    @abc.abstractmethod
    async def validate(self, *, server_type: str, version: str) -> None:
        """Pass if the catalog offers ``version`` for ``server_type``; else raise.

        Raises :class:`UnsupportedServerTypeError` for a type the catalog cannot
        resolve at M1 (forge) and :class:`UnknownVersionError` for an unoffered
        version. A transient source outage is *not* swallowed here — the adapter
        lets it surface so create fails loudly rather than admitting an unvalidated
        version.
        """
