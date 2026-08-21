"""Post-restore plugin reconcile against a real session (issue #2612).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). ``tests/servers/test_backup_plugin_
reconcile.py`` covers the reconcile's rules against fakes; what needs a real
session is the *failure* rule: the ghost loop's ``except Exception: continue``
is only a per-jar skip if the session survives the jar it skipped.

A staged INSERT that violates a constraint deactivates the SQLAlchemy
transaction, so every later statement raises ``PendingRollbackError`` -- the
restore then dies on an error naming neither the bad jar nor the constraint,
several statements away from the cause. A fake carries no constraints and no
transaction, so it cannot show this at all (issues #2557, #2549).
"""

from __future__ import annotations

import datetime as dt
import io
import os
import uuid
import zipfile
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.plugin_repository import (
    SqlAlchemyPluginRepository,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.backups import _reconcile_plugins
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeVersionValidator,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _seed_server(engine: AsyncEngine) -> Server:
    community_id = uuid.uuid4()
    community = Community(
        id=CommunityCommunityId(community_id),
        name=CommunityName(f"guild-{community_id}"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    factory = create_session_factory(engine)
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return await CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="paper",
        config={},
    )


def _jar(marker: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        zf.writestr("marker", marker)
    return buf.getvalue()


def _plugin(server_id: ServerId, rel_path: str, filename: str) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=rel_path,
        filename=filename,
        display_name=filename,
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=None,
        checksum_sha512=None,
        sha256=None,
        size_bytes=None,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _InstallOnListRepository(SqlAlchemyPluginRepository):
    """A plugin repository that installs ``plugins/one.jar`` after the reconcile read.

    Reproduces the production interleave deterministically, with no sleeps: the
    reconcile lists the rows it will treat as authoritative, another request's
    install commits a row at one of the ghost paths on its own connection, and
    the ghost INSERT for that path then violates ``uq_server_plugin_server_rel``.
    """

    def __init__(self, session: AsyncSession, engine: AsyncEngine) -> None:
        super().__init__(session)
        self._engine = engine
        self._raced = False

    async def list_for_server(self, server_id: ServerId) -> list[ServerPlugin]:
        rows = await super().list_for_server(server_id)
        if not self._raced:
            self._raced = True
            async with ServersUnitOfWork(create_session_factory(self._engine)) as racer:
                await racer.plugins.add(
                    _plugin(server_id, "plugins/one.jar", "one.jar"),
                )
                await racer.commit()
        return rows


class _RacingUnitOfWork(ServersUnitOfWork):
    """A servers UnitOfWork wired with :class:`_InstallOnListRepository`."""

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(create_session_factory(engine))
        self._engine = engine

    async def __aenter__(self) -> "_RacingUnitOfWork":
        await super().__aenter__()
        assert self._session is not None
        self.plugins = _InstallOnListRepository(self._session, self._engine)
        return self


async def test_a_ghost_that_violates_a_constraint_does_not_poison_the_reconcile(
    engine: AsyncEngine,
) -> None:
    # The first ghost's INSERT is refused by uq_server_plugin_server_rel. Only
    # that jar may be skipped: the second ghost must still be ingested and the
    # reconcile must still commit. Unwrapped, the refusal surfaced at a later
    # jar's autoflush, was swallowed by the loop's ``except Exception``, and left
    # the session in pending-rollback, so the reconcile died on a
    # PendingRollbackError that named neither jar.
    server = await _seed_server(engine)
    file_store = FakeFileStore()
    file_store.files["plugins/one.jar"] = _jar(b"one")
    file_store.files["plugins/two.jar"] = _jar(b"two")

    await _reconcile_plugins(
        uow=_RacingUnitOfWork(engine),
        file_store=file_store,
        cache=None,
        clock=FakeClock(_NOW),
        community_id=server.community_id,
        server_id=server.id,
        server=server,
    )

    async with ServersUnitOfWork(create_session_factory(engine)) as uow:
        rows = await uow.plugins.list_for_server(server.id)
    assert sorted(row.rel_path for row in rows) == [
        "plugins/one.jar",
        "plugins/two.jar",
    ]
