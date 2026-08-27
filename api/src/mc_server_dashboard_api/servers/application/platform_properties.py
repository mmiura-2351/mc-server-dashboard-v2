"""Shared reads behind the platform half of ``server.properties`` (issue #2810).

Several write paths publish what the DB says about a server into its
``server.properties``: the restore re-applies every platform-managed key
(:class:`RestoreBackup`, #2621), a config-overrides ``PATCH`` seeds them when the
file is absent (:class:`UpdateServer`), and the resource-pack assign does the same
(:class:`AssignResourcePack`). Each composes its own write — they differ in what
else they apply and in how a storage failure is surfaced — but the two reads they
share live here once rather than once per call site:

- :func:`read_properties` — the file, or ``None`` when there is none to rewrite.
- :func:`assigned_resource_pack` — the assignment row as properties values.

:func:`pack_download_url` lives here too, because it is what turns the assignment
row into the ``resource-pack`` line every one of those paths writes.
"""

from __future__ import annotations

from urllib.parse import quote

from mc_server_dashboard_api.servers.domain.errors import ServerFileNotFoundError
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId
from mc_server_dashboard_api.servers.domain.server_properties import (
    ResourcePackProperties,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

_PROPERTIES_REL_PATH = "server.properties"


def pack_download_url(
    public_base_url: str, pack_id: ResourcePackId, filename: str
) -> str:
    return (
        f"{public_base_url}/api/public/resource-packs/"
        f"{pack_id.value}/{quote(filename, safe='')}"
    )


async def read_properties(
    file_store: FileStore, *, community_id: CommunityId, server_id: ServerId
) -> bytes | None:
    """Return the at-rest ``server.properties``, or ``None`` when it is absent.

    Absent is kept distinct from empty on purpose. A caller that rewrites keys in
    an existing file must instead seed the whole platform half when there is no
    file to rewrite (issue #2810) — treating absent as ``b""`` is exactly the bug:
    it publishes a file holding only the keys that caller writes, with no
    ``rcon.password`` for the worker to reach the server with and no
    ``server-port``, so the server silently binds Mojang's 25565.

    Only a genuinely missing file is reported as ``None``; every other storage
    failure propagates, so each caller keeps its own posture on those.
    """

    try:
        return await file_store.read_file(
            community_id=community_id,
            server_id=server_id,
            rel_path=_PROPERTIES_REL_PATH,
        )
    except ServerFileNotFoundError:
        return None


async def assigned_resource_pack(
    uow: UnitOfWork, *, server_id: ServerId, public_base_url: str
) -> ResourcePackProperties | None:
    """The server's currently assigned pack as properties values, or ``None``.

    ``None`` — no assignment row, or a row whose pack has gone — means the server
    has no pack, which :func:`apply_platform_properties` writes by clearing all
    four resource-pack keys.
    """

    async with uow:
        assignment = await uow.resource_packs.get_assignment_by_server(server_id)
        if assignment is None:
            return None
        pack = await uow.resource_packs.get_by_id(assignment.resource_pack_id)
    if pack is None:
        return None
    return ResourcePackProperties(
        url=pack_download_url(public_base_url, pack.id, pack.filename),
        sha1=pack.sha1_hash,
        require=assignment.require_resource_pack,
        prompt=assignment.resource_pack_prompt,
    )
