"""Endpoint tests for the mod library (issue #1261).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases faked (NFR-TEST-1, no database). Verifies:

- upload 201, list 200 (+ filters), delete 204, download 200;
- authorization gate (upload requires server:update in any community);
- error mapping (not found -> 404, permission denied -> 403, too large -> 413,
  bad extension -> 422);
- dedup is transparent at the edge (the use case returns the existing entry).
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
import zipfile
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.dependencies import (
    get_assign_mods,
    get_audit_recorder,
    get_current_user,
    get_delete_mod,
    get_download_client_modpack,
    get_download_mod,
    get_list_client_mods,
    get_list_mods,
    get_list_server_mods,
    get_mod_store,
    get_set_mod_enabled,
    get_unassign_mod,
    get_upload_mod,
    require_server_update_in_any_community,
)
from mc_server_dashboard_api.servers.application.mod_resolution import (
    ResolutionEntry,
    ResolutionPlan,
)
from mc_server_dashboard_api.servers.application.mod_validation import (
    LoaderMismatch,
    McMismatch,
    MissingDependency,
    ModValidation,
    VersionUnsatisfied,
)
from mc_server_dashboard_api.servers.application.mods import UploadMod
from mc_server_dashboard_api.servers.application.server_mods import ServerModSet
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    ModAssignmentNotFoundError,
    ModInUseError,
    ModNotFoundError,
    PermissionDeniedError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId
from mc_server_dashboard_api.servers.domain.server_mod import (
    ServerModAssignment,
    ServerModId,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.audit.fakes import RecordingAuditRecorder
from tests.identity.fakes import make_user
from tests.servers.fakes import FakeClock, FakeModStore, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 16, 12, 0, 0, tzinfo=dt.timezone.utc)
_MOD_ID = ModId(uuid.UUID("11111111-1111-1111-1111-111111111111"))


def _mod(
    *,
    mod_id: ModId | None = None,
    filename: str = "my-mod.jar",
    display_name: str = "My Mod",
    loader_type: str = "fabric",
    side: str = "both",
    uploaded_by: uuid.UUID | None = None,
) -> Mod:
    return Mod(
        id=mod_id or _MOD_ID,
        filename=filename,
        display_name=display_name,
        description=None,
        loader_type=loader_type,  # type: ignore[arg-type]
        mod_identifier="examplemod",
        provides=[],
        version_number="1.0.0",
        mc_versions=["1.20.4"],
        side=side,  # type: ignore[arg-type]
        dependencies=[],
        sha256_hash="def456",
        sha512_hash="ghi789",
        size_bytes=1234,
        source="local",
        source_project_id=None,
        source_version_id=None,
        uploaded_by=uploaded_by or uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


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


class _FakeDownloadUseCase:
    """Fake that returns a (stream, mod) tuple like DownloadMod."""

    def __init__(
        self,
        *,
        mod: Mod | None = None,
        data: bytes = b"jardata",
        error: Exception | None = None,
    ):
        self._mod = mod or _mod()
        self._data = data
        self._error = error

    async def __call__(self, **kwargs: object) -> tuple[AsyncIterator[bytes], Mod]:
        if self._error is not None:
            raise self._error

        async def _stream() -> AsyncIterator[bytes]:
            yield self._data

        return _stream(), self._mod


def _app(
    *,
    upload: _FakeUseCase | None = None,
    list_: _FakeUseCase | None = None,
    delete: _FakeUseCase | None = None,
    download: _FakeDownloadUseCase | None = None,
    recorder: RecordingAuditRecorder | None = None,
    is_admin: bool = False,
    require_upload_perm: bool = True,
    store: FakeModStore | None = None,
) -> object:
    app = create_app()
    user = make_user(is_platform_admin=is_admin)
    app.dependency_overrides[get_current_user] = lambda: user
    if require_upload_perm:
        app.dependency_overrides[require_server_update_in_any_community] = lambda: user
    else:
        from fastapi import status

        from mc_server_dashboard_api.http_problem import problem

        async def _deny() -> None:
            raise problem(status.HTTP_403_FORBIDDEN, "forbidden")

        app.dependency_overrides[require_server_update_in_any_community] = _deny

    if upload is not None:
        app.dependency_overrides[get_upload_mod] = lambda: upload
    if list_ is not None:
        app.dependency_overrides[get_list_mods] = lambda: list_
    if delete is not None:
        app.dependency_overrides[get_delete_mod] = lambda: delete
    if download is not None:
        app.dependency_overrides[get_download_mod] = lambda: download
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    if store is not None:
        app.dependency_overrides[get_mod_store] = lambda: store
    return app


class TestUploadEndpoint:
    def test_upload_201(self) -> None:
        m = _mod()
        uc = _FakeUseCase(result=m)
        recorder = RecordingAuditRecorder()
        app = _app(upload=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods",
                data={"display_name": "My Mod"},
                files={
                    "file": (
                        "my-mod.jar",
                        io.BytesIO(b"PK\x03\x04"),
                        "application/java-archive",
                    )
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == str(m.id.value)
        assert body["filename"] == "my-mod.jar"
        assert body["loader_type"] == "fabric"
        assert body["side"] == "both"
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == ops.MOD_UPLOAD
        assert recorder.events[0].outcome == Outcome.SUCCESS

    def test_upload_forwards_side_override(self) -> None:
        uc = _FakeUseCase(result=_mod(side="server"))
        app = _app(upload=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods",
                data={"display_name": "My Mod", "side": "server"},
                files={
                    "file": (
                        "my-mod.jar",
                        io.BytesIO(b"PK\x03\x04"),
                        "application/java-archive",
                    )
                },
            )
        assert resp.status_code == 201
        assert uc.calls[0]["side"] == "server"

    def test_upload_basename_constrains_path_like_filename(self) -> None:
        # A path-like filename is reduced to a bare basename before it reaches the
        # use case, so the deploy target / Content-Disposition never see a path
        # (issue #1278).
        for raw, expected in [
            ("a/b.jar", "b.jar"),
            ("../x.jar", "x.jar"),
            ("/abs/path/mod.jar", "mod.jar"),
            ("a\\b.jar", "b.jar"),
        ]:
            uc = _FakeUseCase(result=_mod())
            app = _app(upload=uc)
            with TestClient(app) as client:  # type: ignore[arg-type]
                resp = client.post(
                    "/api/mods",
                    data={"display_name": "My Mod"},
                    files={
                        "file": (
                            raw,
                            io.BytesIO(b"PK\x03\x04"),
                            "application/java-archive",
                        )
                    },
                )
            assert resp.status_code == 201, raw
            assert uc.calls[0]["filename"] == expected, raw

    def test_upload_rejects_filename_with_no_basename(self) -> None:
        # A filename that reduces to nothing usable (empty / "." / "..") is a clean
        # 4xx, never forwarded as a path (issue #1278).
        for raw in ["..", "foo/.."]:
            uc = _FakeUseCase(result=_mod())
            app = _app(upload=uc)
            with TestClient(app) as client:  # type: ignore[arg-type]
                resp = client.post(
                    "/api/mods",
                    data={"display_name": "Bad"},
                    files={
                        "file": (
                            raw,
                            io.BytesIO(b"PK\x03\x04"),
                            "application/java-archive",
                        )
                    },
                )
            assert resp.status_code == 422, raw
            assert uc.calls == [], raw

    def test_upload_non_jar_422(self) -> None:
        uc = _FakeUseCase()
        app = _app(upload=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods",
                data={"display_name": "Bad"},
                files={"file": ("bad.zip", io.BytesIO(b"data"), "application/zip")},
            )
        assert resp.status_code == 422

    def test_upload_too_large_413(self) -> None:
        uc = _FakeUseCase(error=FileTooLargeError("too big"))
        app = _app(upload=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods",
                data={"display_name": "Big"},
                files={
                    "file": (
                        "big.jar",
                        io.BytesIO(b"PK\x03\x04"),
                        "application/java-archive",
                    )
                },
            )
        assert resp.status_code == 413

    def test_upload_unrecognized_jar_422(self) -> None:
        # A readable jar with no recognized manifest reaches the real use case,
        # which rejects it (no determinable loader); the edge maps it to 422.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.txt", "no manifest here")
        uc = UploadMod(
            uow=FakeUnitOfWork(),
            store=FakeModStore(),
            clock=FakeClock(_NOW),
        )
        app = _app(upload=uc)  # type: ignore[arg-type]
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods",
                data={"display_name": "Plain"},
                files={
                    "file": (
                        "plain.jar",
                        io.BytesIO(buf.getvalue()),
                        "application/java-archive",
                    )
                },
            )
        assert resp.status_code == 422
        assert resp.json()["reason"] == "invalid_mod_jar"

    def test_upload_denied_403(self) -> None:
        uc = _FakeUseCase()
        app = _app(upload=uc, require_upload_perm=False)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods",
                data={"display_name": "No Perm"},
                files={
                    "file": (
                        "mod.jar",
                        io.BytesIO(b"data"),
                        "application/java-archive",
                    )
                },
            )
        assert resp.status_code == 403


class TestListEndpoint:
    def test_list_200(self) -> None:
        mods = [_mod(filename="a.jar"), _mod(filename="b.jar", mod_id=ModId.new())]
        uc = _FakeUseCase(result=mods)
        app = _app(list_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/mods")
        assert resp.status_code == 200
        assert len(resp.json()["mods"]) == 2

    def test_list_empty_200(self) -> None:
        uc = _FakeUseCase(result=[])
        app = _app(list_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/mods")
        assert resp.status_code == 200
        assert resp.json()["mods"] == []

    def test_list_forwards_filters(self) -> None:
        uc = _FakeUseCase(result=[])
        app = _app(list_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/mods?loader=fabric&mc=1.20.4&side=client")
        assert resp.status_code == 200
        assert uc.calls[0] == {
            "loader_type": "fabric",
            "mc_version": "1.20.4",
            "side": "client",
        }


class TestDeleteEndpoint:
    def test_delete_204(self) -> None:
        uc = _FakeUseCase()
        recorder = RecordingAuditRecorder()
        app = _app(delete=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"/api/mods/{_MOD_ID.value}")
        assert resp.status_code == 204
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == ops.MOD_DELETE

    def test_delete_not_found_404(self) -> None:
        uc = _FakeUseCase(error=ModNotFoundError("not found"))
        app = _app(delete=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"/api/mods/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_delete_forbidden_403(self) -> None:
        uc = _FakeUseCase(error=PermissionDeniedError("nope"))
        app = _app(delete=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"/api/mods/{uuid.uuid4()}")
        assert resp.status_code == 403

    def test_delete_in_use_409(self) -> None:
        uc = _FakeUseCase(error=ModInUseError("assigned"))
        app = _app(delete=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"/api/mods/{uuid.uuid4()}")
        assert resp.status_code == 409
        assert resp.json()["reason"] == "mod_in_use"


class TestDownloadEndpoint:
    def test_download_200(self) -> None:
        m = _mod()
        uc = _FakeDownloadUseCase(mod=m, data=b"jarbytes")
        recorder = RecordingAuditRecorder()
        app = _app(download=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(f"/api/mods/{m.id.value}/download")
        assert resp.status_code == 200
        assert resp.content == b"jarbytes"
        assert resp.headers["content-type"] == "application/java-archive"
        assert "attachment" in resp.headers["content-disposition"]
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == ops.MOD_DOWNLOAD

    def test_download_not_found_404(self) -> None:
        uc = _FakeDownloadUseCase(error=ModNotFoundError("nope"))
        app = _app(download=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(f"/api/mods/{uuid.uuid4()}/download")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Assignment endpoint tests (issue #1262)
# ---------------------------------------------------------------------------

_COMMUNITY_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_SERVER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_ASSIGN_BASE = f"/api/communities/{_COMMUNITY_ID}/servers/{_SERVER_ID}/mods"


def _assignment(mod: Mod) -> ServerModAssignment:
    return ServerModAssignment(
        id=ServerModId.new(),
        server_id=ServerId(_SERVER_ID),
        mod_id=mod.id,
        enabled=True,
        assigned_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _mod_set(
    entries: list[tuple[ServerModAssignment, Mod]],
    validation: ModValidation | None = None,
) -> ServerModSet:
    return ServerModSet(entries=entries, validation=validation or ModValidation())


def _assignment_app(
    *,
    assign: _FakeUseCase | None = None,
    unassign: _FakeUseCase | None = None,
    toggle: _FakeUseCase | None = None,
    list_: _FakeUseCase | None = None,
    resolve: _FakeUseCase | None = None,
    apply_resolution: _FakeUseCase | None = None,
    client_list: _FakeUseCase | None = None,
    modpack: object | None = None,
    recorder: RecordingAuditRecorder | None = None,
    permit: bool = True,
) -> object:
    from mc_server_dashboard_api.community.domain.permission_checker import (
        MembershipVisibility,
        PermissionChecker,
    )
    from mc_server_dashboard_api.dependencies import (
        get_apply_server_mod_resolution,
        get_membership_visibility,
        get_permission_checker,
        get_resolve_server_mods,
    )

    app = create_app()
    user = make_user()
    app.dependency_overrides[get_current_user] = lambda: user

    if assign is not None:
        app.dependency_overrides[get_assign_mods] = lambda: assign
    if unassign is not None:
        app.dependency_overrides[get_unassign_mod] = lambda: unassign
    if toggle is not None:
        app.dependency_overrides[get_set_mod_enabled] = lambda: toggle
    if list_ is not None:
        app.dependency_overrides[get_list_server_mods] = lambda: list_
    if resolve is not None:
        app.dependency_overrides[get_resolve_server_mods] = lambda: resolve
    if apply_resolution is not None:
        app.dependency_overrides[get_apply_server_mod_resolution] = lambda: (
            apply_resolution
        )
    if client_list is not None:
        app.dependency_overrides[get_list_client_mods] = lambda: client_list
    if modpack is not None:
        app.dependency_overrides[get_download_client_modpack] = lambda: modpack
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder

    class _AlwaysMember(MembershipVisibility):
        async def is_member(self, *, user_id: object, community_id: object) -> bool:
            return True

    class _Checker(PermissionChecker):
        async def can(
            self, *, user: object, operation: object, resource: object
        ) -> bool:
            return permit

    app.dependency_overrides[get_membership_visibility] = _AlwaysMember
    app.dependency_overrides[get_permission_checker] = _Checker

    return app


class TestAssignEndpoint:
    def test_assign_201(self) -> None:
        m = _mod()
        assign = _FakeUseCase(result=[_assignment(m)])
        list_ = _FakeUseCase(result=_mod_set([(_assignment(m), m)]))
        recorder = RecordingAuditRecorder()
        app = _assignment_app(assign=assign, list_=list_, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(_ASSIGN_BASE, json={"mod_ids": [str(m.id.value)]})
        assert resp.status_code == 201
        body = resp.json()
        assert len(body["mods"]) == 1
        assert body["mods"][0]["mod"]["id"] == str(m.id.value)
        assert body["mods"][0]["enabled"] is True
        assert body["validation"] == {
            "missing_deps": [],
            "version_unsatisfied": [],
            "conflicts": [],
            "loader_mismatch": [],
            "mc_mismatch": [],
        }
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == ops.MOD_ASSIGN

    def test_assign_server_not_found_404(self) -> None:
        assign = _FakeUseCase(error=ServerNotFoundError("nope"))
        list_ = _FakeUseCase(result=[])
        app = _assignment_app(assign=assign, list_=list_)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(_ASSIGN_BASE, json={"mod_ids": [str(uuid.uuid4())]})
        assert resp.status_code == 404

    def test_assign_mod_not_found_404(self) -> None:
        assign = _FakeUseCase(error=ModNotFoundError("nope"))
        list_ = _FakeUseCase(result=[])
        app = _assignment_app(assign=assign, list_=list_)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(_ASSIGN_BASE, json={"mod_ids": [str(uuid.uuid4())]})
        assert resp.status_code == 404

    def test_assign_unsettled_409(self) -> None:
        assign = _FakeUseCase(error=ServerFilesUnsettledError("nope"))
        list_ = _FakeUseCase(result=[])
        app = _assignment_app(assign=assign, list_=list_)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(_ASSIGN_BASE, json={"mod_ids": [str(uuid.uuid4())]})
        assert resp.status_code == 409
        assert resp.json()["reason"] == "server_unsettled"


class TestUnassignEndpoint:
    def test_unassign_204(self) -> None:
        uc = _FakeUseCase()
        recorder = RecordingAuditRecorder()
        app = _assignment_app(unassign=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"{_ASSIGN_BASE}/{uuid.uuid4()}")
        assert resp.status_code == 204
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == ops.MOD_UNASSIGN

    def test_unassign_not_found_404(self) -> None:
        uc = _FakeUseCase(error=ModAssignmentNotFoundError("nope"))
        app = _assignment_app(unassign=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"{_ASSIGN_BASE}/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_unassign_unsettled_409(self) -> None:
        uc = _FakeUseCase(error=ServerFilesUnsettledError("nope"))
        app = _assignment_app(unassign=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"{_ASSIGN_BASE}/{uuid.uuid4()}")
        assert resp.status_code == 409


class TestToggleEndpoint:
    def test_enable_204(self) -> None:
        m = _mod()
        uc = _FakeUseCase(result=_assignment(m))
        recorder = RecordingAuditRecorder()
        app = _assignment_app(toggle=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(f"{_ASSIGN_BASE}/{m.id.value}/enable")
        assert resp.status_code == 204
        assert recorder.events[0].operation == ops.MOD_ENABLE

    def test_disable_204(self) -> None:
        m = _mod()
        uc = _FakeUseCase(result=_assignment(m))
        recorder = RecordingAuditRecorder()
        app = _assignment_app(toggle=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(f"{_ASSIGN_BASE}/{m.id.value}/disable")
        assert resp.status_code == 204
        assert recorder.events[0].operation == ops.MOD_DISABLE

    def test_toggle_not_found_404(self) -> None:
        uc = _FakeUseCase(error=ModAssignmentNotFoundError("nope"))
        app = _assignment_app(toggle=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(f"{_ASSIGN_BASE}/{uuid.uuid4()}/enable")
        assert resp.status_code == 404

    def test_toggle_unsettled_409(self) -> None:
        uc = _FakeUseCase(error=ServerFilesUnsettledError("nope"))
        app = _assignment_app(toggle=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(f"{_ASSIGN_BASE}/{uuid.uuid4()}/disable")
        assert resp.status_code == 409


class TestListServerModsEndpoint:
    def test_list_200(self) -> None:
        m = _mod()
        uc = _FakeUseCase(result=_mod_set([(_assignment(m), m)]))
        app = _assignment_app(list_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_ASSIGN_BASE)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["mods"]) == 1
        assert body["mods"][0]["mod"]["id"] == str(m.id.value)

    def test_list_empty_200(self) -> None:
        uc = _FakeUseCase(result=_mod_set([]))
        app = _assignment_app(list_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_ASSIGN_BASE)
        assert resp.status_code == 200
        assert resp.json()["mods"] == []

    def test_list_includes_validation_block(self) -> None:
        """The mod-set response carries the phase-B validation checklist (#1263)."""

        m = _mod()
        validation = ModValidation(
            missing_deps=[
                MissingDependency(
                    mod_id="examplemod",
                    depends_on="fabric-api",
                    version_range=">=0.90.0",
                )
            ],
            version_unsatisfied=[
                VersionUnsatisfied(
                    mod_id="examplemod",
                    depends_on="forge-lib",
                    version_range="[2.0,)",
                    present_version="1.5",
                )
            ],
            loader_mismatch=[
                LoaderMismatch(
                    mod_id="examplemod",
                    mod_loader="forge",
                    server_loader="fabric",
                )
            ],
            mc_mismatch=[
                McMismatch(
                    mod_id="examplemod",
                    mod_mc_versions=["1.20.4"],
                    server_mc_version="1.21",
                )
            ],
        )
        uc = _FakeUseCase(result=_mod_set([(_assignment(m), m)], validation))
        app = _assignment_app(list_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_ASSIGN_BASE)
        assert resp.status_code == 200
        block = resp.json()["validation"]
        assert block["missing_deps"] == [
            {
                "mod_id": "examplemod",
                "depends_on": "fabric-api",
                "version_range": ">=0.90.0",
            }
        ]
        assert block["loader_mismatch"] == [
            {
                "mod_id": "examplemod",
                "mod_loader": "forge",
                "server_loader": "fabric",
            }
        ]
        assert block["mc_mismatch"] == [
            {
                "mod_id": "examplemod",
                "mod_mc_versions": ["1.20.4"],
                "server_mc_version": "1.21",
            }
        ]
        assert block["version_unsatisfied"] == [
            {
                "mod_id": "examplemod",
                "depends_on": "forge-lib",
                "version_range": "[2.0,)",
                "present_version": "1.5",
            }
        ]
        assert block["conflicts"] == []

    def test_list_server_not_found_404(self) -> None:
        uc = _FakeUseCase(error=ServerNotFoundError("nope"))
        app = _assignment_app(list_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_ASSIGN_BASE)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Dependency resolution endpoint tests (issue #1294)
# ---------------------------------------------------------------------------

_RESOLVE_BASE = f"{_ASSIGN_BASE}/resolve"


def _plan(
    entries: list[ResolutionEntry] | None = None,
    validation: ModValidation | None = None,
) -> ResolutionPlan:
    return ResolutionPlan(
        entries=entries or [], validation=validation or ModValidation()
    )


class TestResolvePlanEndpoint:
    def test_plan_200_classifies_entries(self) -> None:
        m = _mod(filename="fabric-api.jar")
        plan = _plan(
            entries=[
                ResolutionEntry(
                    dep_identifier="fabric-api",
                    required_range=">=0.90.0",
                    status="resolvable_from_library",
                    mod=m,
                ),
                ResolutionEntry(
                    dep_identifier="some-lib",
                    required_range="",
                    status="needs_import",
                ),
            ]
        )
        uc = _FakeUseCase(result=plan)
        app = _assignment_app(resolve=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_RESOLVE_BASE)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["entries"]) == 2
        assert body["entries"][0]["dep_identifier"] == "fabric-api"
        assert body["entries"][0]["status"] == "resolvable_from_library"
        assert body["entries"][0]["mod"]["id"] == str(m.id.value)
        assert body["entries"][1]["status"] == "needs_import"
        assert body["entries"][1]["mod"] is None
        assert body["validation"]["missing_deps"] == []

    def test_plan_server_not_found_404(self) -> None:
        uc = _FakeUseCase(error=ServerNotFoundError("nope"))
        app = _assignment_app(resolve=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_RESOLVE_BASE)
        assert resp.status_code == 404

    def test_plan_requires_server_read_403(self) -> None:
        uc = _FakeUseCase(result=_plan())
        app = _assignment_app(resolve=uc, permit=False)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_RESOLVE_BASE)
        assert resp.status_code == 403


class TestApplyResolutionEndpoint:
    def test_apply_200_assigns_and_audits(self) -> None:
        m = _mod(filename="fabric-api.jar")
        plan = _plan(
            entries=[
                ResolutionEntry(
                    dep_identifier="fabric-api",
                    required_range="",
                    status="already_satisfied",
                )
            ]
        )
        uc = _FakeUseCase(result=(plan, [m.id]))
        recorder = RecordingAuditRecorder()
        app = _assignment_app(apply_resolution=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(_RESOLVE_BASE)
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"][0]["status"] == "already_satisfied"
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == ops.MOD_RESOLVE

    def test_apply_unsettled_409(self) -> None:
        uc = _FakeUseCase(error=ServerFilesUnsettledError("nope"))
        app = _assignment_app(apply_resolution=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(_RESOLVE_BASE)
        assert resp.status_code == 409
        assert resp.json()["reason"] == "server_unsettled"

    def test_apply_server_not_found_404(self) -> None:
        uc = _FakeUseCase(error=ServerNotFoundError("nope"))
        app = _assignment_app(apply_resolution=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(_RESOLVE_BASE)
        assert resp.status_code == 404

    def test_apply_requires_server_update_403(self) -> None:
        uc = _FakeUseCase(result=(_plan(), []))
        app = _assignment_app(apply_resolution=uc, permit=False)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(_RESOLVE_BASE)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Client modpack endpoint tests (issue #1265)
# ---------------------------------------------------------------------------

_CLIENT_BASE = f"/api/communities/{_COMMUNITY_ID}/servers/{_SERVER_ID}/client-mods"


class _FakeModpackUseCase:
    """Fake that returns a (stream, server_name) tuple like DownloadClientModpack."""

    def __init__(
        self,
        *,
        data: bytes = b"PK-zip-bytes",
        server_name: str = "my-server",
        error: Exception | None = None,
    ):
        self._data = data
        self._server_name = server_name
        self._error = error

    async def __call__(self, **kwargs: object) -> tuple[AsyncIterator[bytes], str]:
        if self._error is not None:
            raise self._error

        async def _stream() -> AsyncIterator[bytes]:
            yield self._data

        return _stream(), self._server_name


class TestListClientModsEndpoint:
    def test_list_200(self) -> None:
        client_mod = _mod(side="both", filename="fabric-api.jar")
        uc = _FakeUseCase(result=[client_mod])
        app = _assignment_app(client_list=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_CLIENT_BASE)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["mods"]) == 1
        assert body["mods"][0]["filename"] == "fabric-api.jar"
        assert body["mods"][0]["side"] == "both"

    def test_list_empty_200(self) -> None:
        uc = _FakeUseCase(result=[])
        app = _assignment_app(client_list=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_CLIENT_BASE)
        assert resp.status_code == 200
        assert resp.json()["mods"] == []

    def test_list_server_not_found_404(self) -> None:
        uc = _FakeUseCase(error=ServerNotFoundError("nope"))
        app = _assignment_app(client_list=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_CLIENT_BASE)
        assert resp.status_code == 404

    def test_list_requires_server_read_403(self) -> None:
        uc = _FakeUseCase(result=[])
        app = _assignment_app(client_list=uc, permit=False)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(_CLIENT_BASE)
        assert resp.status_code == 403


class TestDownloadClientModpackEndpoint:
    def test_download_200_streams_zip(self) -> None:
        uc = _FakeModpackUseCase(data=b"ZIPDATA", server_name="alpha")
        app = _assignment_app(modpack=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(f"{_CLIENT_BASE}/download")
        assert resp.status_code == 200
        assert resp.content == b"ZIPDATA"
        assert resp.headers["content-type"] == "application/zip"
        disposition = resp.headers["content-disposition"]
        assert "attachment" in disposition
        assert "alpha-client-mods.zip" in disposition

    def test_download_server_not_found_404(self) -> None:
        uc = _FakeModpackUseCase(error=ServerNotFoundError("nope"))
        app = _assignment_app(modpack=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(f"{_CLIENT_BASE}/download")
        assert resp.status_code == 404

    def test_download_requires_server_read_403(self) -> None:
        uc = _FakeModpackUseCase()
        app = _assignment_app(modpack=uc, permit=False)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(f"{_CLIENT_BASE}/download")
        assert resp.status_code == 403
