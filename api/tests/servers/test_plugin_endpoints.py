"""Endpoint tests for the plugin routes (issue #1163).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- the two-layer gate per route (non-member -> 404, member-without-permission ->
  403, authorized member -> 2xx);
- domain-error -> HTTP-code mapping (server_unsettled 409, file_too_large 413,
  checksum_mismatch 502, etc.);
- correct status codes for each plugin endpoint.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceRef,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_check_plugin_update,
    get_check_updates,
    get_current_user,
    get_download_client_modpack,
    get_get_plugin,
    get_install_plugin,
    get_list_client_mods,
    get_list_plugin_dependencies,
    get_list_plugins,
    get_membership_visibility,
    get_permission_checker,
    get_remove_plugin,
    get_set_plugin_side,
    get_toggle_plugin,
    get_update_plugin,
    get_validate_plugin_set,
)
from mc_server_dashboard_api.servers.application.catalog import (
    PluginDependencyInfo,
    PluginUpdateInfo,
)
from mc_server_dashboard_api.servers.application.plugin_validation import (
    McMismatch,
    MissingDependency,
    PluginValidation,
)
from mc_server_dashboard_api.servers.domain.errors import (
    CatalogChecksumMismatchError,
    FileTooLargeError,
    InvalidPluginSideError,
    PluginAlreadyExistsError,
    PluginNotFoundError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.identity.fakes import make_user

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


def _plugin(
    *,
    server_id: uuid.UUID | None = None,
    plugin_id: uuid.UUID | None = None,
    side: str = "both",
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId(plugin_id or uuid.uuid4()),
        server_id=ServerId(server_id or uuid.uuid4()),
        rel_path="plugins/test.jar",
        filename="test.jar",
        display_name="Test Plugin",
        description="A test plugin",
        loader_type=LoaderType.PLUGIN,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=None,
        checksum_sha512="abc123",
        sha256="def456",
        size_bytes=1024,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        side=side,  # type: ignore[arg-type]
    )


class _FakeVisibility(MembershipVisibility):
    def __init__(self, *, member: bool) -> None:
        self._member = member

    async def is_member(self, *, user_id: UserId, community_id: CommunityId) -> bool:
        return self._member


class _FakeChecker(PermissionChecker):
    def __init__(self, *, allow: bool) -> None:
        self._allow = allow

    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        return self._allow


class _FakeUseCase:
    def __init__(self, *, result: object = None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


class _FakeInstall:
    """Fake for :class:`InstallPlugin` which accepts multipart-style kwargs."""

    def __init__(self, *, result: object = None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(
    *,
    member: bool,
    allow: bool,
    list_: _FakeUseCase | None = None,
    get: _FakeUseCase | None = None,
    install: _FakeInstall | None = None,
    remove: _FakeUseCase | None = None,
    toggle: _FakeUseCase | None = None,
    check_updates: _FakeUseCase | None = None,
    check_plugin_update: _FakeUseCase | None = None,
    update: _FakeUseCase | None = None,
    list_deps: _FakeUseCase | None = None,
    validate: _FakeUseCase | None = None,
    set_side: _FakeUseCase | None = None,
    list_client: _FakeUseCase | None = None,
    download_modpack: _FakeUseCase | None = None,
) -> object:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if list_ is not None:
        app.dependency_overrides[get_list_plugins] = lambda: list_
    if get is not None:
        app.dependency_overrides[get_get_plugin] = lambda: get
    if install is not None:
        app.dependency_overrides[get_install_plugin] = lambda: install
    if remove is not None:
        app.dependency_overrides[get_remove_plugin] = lambda: remove
    if toggle is not None:
        app.dependency_overrides[get_toggle_plugin] = lambda: toggle
    if check_updates is not None:
        app.dependency_overrides[get_check_updates] = lambda: check_updates
    if check_plugin_update is not None:
        app.dependency_overrides[get_check_plugin_update] = lambda: check_plugin_update
    if update is not None:
        app.dependency_overrides[get_update_plugin] = lambda: update
    if list_deps is not None:
        app.dependency_overrides[get_list_plugin_dependencies] = lambda: list_deps
    if validate is not None:
        app.dependency_overrides[get_validate_plugin_set] = lambda: validate
    if set_side is not None:
        app.dependency_overrides[get_set_plugin_side] = lambda: set_side
    if list_client is not None:
        app.dependency_overrides[get_list_client_mods] = lambda: list_client
    if download_modpack is not None:
        app.dependency_overrides[get_download_client_modpack] = lambda: download_modpack
    return app


def _url(community: uuid.UUID, server: uuid.UUID, suffix: str = "") -> str:
    return f"/api/communities/{community}/servers/{server}/plugins{suffix}"


# --- two-layer gate --------------------------------------------------------


def test_non_member_gets_404_on_list() -> None:
    app = _app(member=False, allow=True, list_=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_install() -> None:
    app = _app(member=True, allow=False, install=_FakeInstall())
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4()),
        data={"display_name": "Test"},
        files={"file": ("test.jar", b"jar-bytes", "application/java-archive")},
    )
    assert resp.status_code == 403


def test_member_without_permission_gets_403_on_remove() -> None:
    app = _app(member=True, allow=False, remove=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}"))
    assert resp.status_code == 403


# --- list plugins ----------------------------------------------------------


def test_list_plugins_returns_200() -> None:
    p = _plugin()
    app = _app(member=True, allow=True, list_=_FakeUseCase(result=[p]))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["plugins"]) == 1
    assert body["plugins"][0]["filename"] == "test.jar"


def test_list_plugins_server_not_found_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        list_=_FakeUseCase(error=ServerNotFoundError("x")),
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 404


# --- get plugin ------------------------------------------------------------


def test_get_plugin_returns_200() -> None:
    p = _plugin()
    app = _app(member=True, allow=True, get=_FakeUseCase(result=p))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), f"/{p.id.value}"))
    assert resp.status_code == 200
    assert resp.json()["filename"] == "test.jar"


def test_get_plugin_not_found_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        get=_FakeUseCase(error=PluginNotFoundError("x")),
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}"))
    assert resp.status_code == 404


# --- install plugin --------------------------------------------------------


def test_install_plugin_returns_201() -> None:
    p = _plugin()
    app = _app(member=True, allow=True, install=_FakeInstall(result=p))
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4()),
        data={"display_name": "Test Plugin"},
        files={"file": ("test.jar", b"jar-bytes", "application/java-archive")},
    )
    assert resp.status_code == 201
    assert resp.json()["filename"] == "test.jar"


def test_install_plugin_unsettled_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        install=_FakeInstall(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4()),
        data={"display_name": "Test"},
        files={"file": ("test.jar", b"jar-bytes", "application/java-archive")},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_install_plugin_too_large_is_413() -> None:
    app = _app(
        member=True,
        allow=True,
        install=_FakeInstall(error=FileTooLargeError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4()),
        data={"display_name": "Test"},
        files={"file": ("test.jar", b"jar-bytes", "application/java-archive")},
    )
    assert resp.status_code == 413
    assert resp.json()["reason"] == "file_too_large"


def test_install_plugin_already_exists_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        install=_FakeInstall(error=PluginAlreadyExistsError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4()),
        data={"display_name": "Test"},
        files={"file": ("test.jar", b"jar-bytes", "application/java-archive")},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "plugin_already_exists"


# --- remove plugin ---------------------------------------------------------


def test_remove_plugin_returns_204() -> None:
    app = _app(member=True, allow=True, remove=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}"))
    assert resp.status_code == 204


def test_remove_plugin_not_found_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        remove=_FakeUseCase(error=PluginNotFoundError("x")),
    )
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}"))
    assert resp.status_code == 404


# --- enable plugin ---------------------------------------------------------


def test_enable_plugin_returns_200() -> None:
    p = _plugin()
    app = _app(member=True, allow=True, toggle=_FakeUseCase(result=p))
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), f"/{p.id.value}/enable"))
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


def test_enable_plugin_unsettled_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        toggle=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/enable"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


# --- disable plugin --------------------------------------------------------


def test_disable_plugin_returns_200() -> None:
    p = _plugin(plugin_id=uuid.uuid4())
    p.enabled = False
    app = _app(member=True, allow=True, toggle=_FakeUseCase(result=p))
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), f"/{p.id.value}/disable"))
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_disable_plugin_unsettled_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        toggle=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/disable"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


# --- check updates (batch) ------------------------------------------------


def test_check_updates_returns_200() -> None:
    p = _plugin()
    info = PluginUpdateInfo(plugin=p, latest_version=None)
    app = _app(member=True, allow=True, check_updates=_FakeUseCase(result=[info]))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/updates"))
    assert resp.status_code == 200
    assert len(resp.json()["updates"]) == 1


# --- check single plugin update -------------------------------------------


def test_check_plugin_update_returns_200() -> None:
    p = _plugin()
    info = PluginUpdateInfo(plugin=p, latest_version=None)
    app = _app(member=True, allow=True, check_plugin_update=_FakeUseCase(result=info))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), f"/{p.id.value}/updates"))
    assert resp.status_code == 200
    assert resp.json()["latest_version"] is None


# --- update plugin ---------------------------------------------------------


def test_update_plugin_returns_200() -> None:
    p = _plugin()
    app = _app(member=True, allow=True, update=_FakeUseCase(result=p))
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), f"/{p.id.value}/update"),
        json={"version_id": "v1"},
    )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "test.jar"


def test_update_plugin_unsettled_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        update=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/update"),
        json={"version_id": "v1"},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_update_plugin_checksum_mismatch_is_502() -> None:
    app = _app(
        member=True,
        allow=True,
        update=_FakeUseCase(error=CatalogChecksumMismatchError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/update"),
        json={"version_id": "v1"},
    )
    assert resp.status_code == 502
    assert resp.json()["reason"] == "checksum_mismatch"


# --- list dependencies ----------------------------------------------------


def test_list_dependencies_returns_200() -> None:
    dep = PluginDependencyInfo(
        project_id="proj-1",
        version_id="ver-1",
        dependency_type="required",
        project_title="Fabric API",
        project_slug="fabric-api",
        installed=True,
    )
    app = _app(member=True, allow=True, list_deps=_FakeUseCase(result=[dep]))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/dependencies"))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["dependencies"]) == 1
    assert body["dependencies"][0]["project_id"] == "proj-1"


# --- validate plugin set (issue #1307) -------------------------------------


def test_validate_plugins_returns_200_with_findings() -> None:
    validation = PluginValidation(
        missing_deps=[
            MissingDependency(
                mod_id="sodium", depends_on="fabric-api", version_range=">=0.90.0"
            )
        ],
        mc_mismatch=[
            McMismatch(
                mod_id="sodium",
                mod_mc_versions=["1.20.4"],
                server_mc_version="1.21",
            )
        ],
    )
    app = _app(member=True, allow=True, validate=_FakeUseCase(result=validation))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/validate"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["missing_deps"][0]["depends_on"] == "fabric-api"
    assert body["mc_mismatch"][0]["server_mc_version"] == "1.21"
    assert body["conflicts"] == []
    assert body["version_unsatisfied"] == []


def test_validate_plugins_empty_is_200() -> None:
    app = _app(
        member=True, allow=True, validate=_FakeUseCase(result=PluginValidation())
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/validate"))
    assert resp.status_code == 200
    assert resp.json()["missing_deps"] == []


def test_validate_plugins_non_member_is_404() -> None:
    app = _app(
        member=False, allow=True, validate=_FakeUseCase(result=PluginValidation())
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/validate"))
    assert resp.status_code == 404


def test_validate_plugins_unsupported_server_type_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        validate=_FakeUseCase(error=UnsupportedPluginServerTypeError("vanilla")),
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/validate"))
    assert resp.status_code == 422


# --- side override (issue #1308) -------------------------------------------


def test_set_side_authorized_returns_plugin() -> None:
    pid = uuid.uuid4()
    p = _plugin(plugin_id=pid, side="both")
    app = _app(member=True, allow=True, set_side=_FakeUseCase(result=p))
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), f"/{pid}/side"), json={"side": "both"}
    )
    assert resp.status_code == 200
    assert resp.json()["side"] == "both"


def test_set_side_invalid_value_is_422() -> None:
    pid = uuid.uuid4()
    app = _app(
        member=True,
        allow=True,
        set_side=_FakeUseCase(error=InvalidPluginSideError("bogus")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), f"/{pid}/side"), json={"side": "bogus"}
    )
    assert resp.status_code == 422


def test_set_side_unsettled_is_409() -> None:
    pid = uuid.uuid4()
    app = _app(
        member=True,
        allow=True,
        set_side=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), f"/{pid}/side"), json={"side": "client"}
    )
    assert resp.status_code == 409


def test_set_side_non_member_is_404() -> None:
    pid = uuid.uuid4()
    app = _app(member=False, allow=True, set_side=_FakeUseCase(result=_plugin()))
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), f"/{pid}/side"), json={"side": "both"}
    )
    assert resp.status_code == 404


def test_set_side_member_without_permission_is_403() -> None:
    pid = uuid.uuid4()
    app = _app(member=True, allow=False, set_side=_FakeUseCase(result=_plugin()))
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), f"/{pid}/side"), json={"side": "both"}
    )
    assert resp.status_code == 403


# --- client modpack (issue #1308) ------------------------------------------


def _client_url(community: uuid.UUID, server: uuid.UUID, suffix: str = "") -> str:
    return f"/api/communities/{community}/servers/{server}/client-mods{suffix}"


def test_list_client_mods_returns_plugins() -> None:
    p = _plugin(side="client")
    app = _app(member=True, allow=True, list_client=_FakeUseCase(result=[p]))
    client = next(_client(app))
    resp = client.get(_client_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["plugins"]) == 1
    assert body["plugins"][0]["side"] == "client"


def test_list_client_mods_non_member_is_404() -> None:
    app = _app(member=False, allow=True, list_client=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(_client_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 404


def test_download_client_modpack_streams_zip() -> None:
    async def _stream() -> object:
        yield b"PK\x03\x04fake-zip"

    app = _app(
        member=True,
        allow=True,
        download_modpack=_FakeUseCase(result=_stream()),
    )
    client = next(_client(app))
    resp = client.get(_client_url(uuid.uuid4(), uuid.uuid4(), "/download"))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert b"fake-zip" in resp.content


def test_download_client_modpack_member_without_permission_is_403() -> None:
    async def _stream() -> object:
        yield b""

    app = _app(
        member=True,
        allow=False,
        download_modpack=_FakeUseCase(result=_stream()),
    )
    client = next(_client(app))
    resp = client.get(_client_url(uuid.uuid4(), uuid.uuid4(), "/download"))
    assert resp.status_code == 403
