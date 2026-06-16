"""Endpoint tests for the resource pack library (issue #1176).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases faked (NFR-TEST-1, no database). Verifies:

- upload 201, list 200, delete 204, download 200, public download 200;
- authorization gate (upload requires server:update in any community);
- error mapping (not found -> 404, permission denied -> 403, in use -> 409);
- public endpoint validates filename match (404 otherwise).
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_current_user,
    get_delete_resource_pack,
    get_download_resource_pack,
    get_list_resource_packs,
    get_resource_pack_store,
    get_upload_resource_pack,
    require_server_update_in_any_community,
)
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    PermissionDeniedError,
    ResourcePackInUseError,
    ResourcePackNotFoundError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.identity.fakes import make_user
from tests.servers.fakes import FakeResourcePackStore

_NOW = dt.datetime(2026, 6, 16, 12, 0, 0, tzinfo=dt.timezone.utc)
_PACK_ID = ResourcePackId(uuid.UUID("11111111-1111-1111-1111-111111111111"))


def _pack(
    *,
    pack_id: ResourcePackId | None = None,
    filename: str = "my-pack.zip",
    uploaded_by: uuid.UUID | None = None,
) -> ResourcePack:
    return ResourcePack(
        id=pack_id or _PACK_ID,
        filename=filename,
        display_name="My Pack",
        description=None,
        sha1_hash="abc123",
        sha256_hash="def456",
        size_bytes=1234,
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
    """Fake that returns a (stream, pack) tuple like DownloadResourcePack."""

    def __init__(
        self,
        *,
        pack: ResourcePack | None = None,
        data: bytes = b"zipdata",
        error: Exception | None = None,
    ):
        self._pack = pack or _pack()
        self._data = data
        self._error = error

    async def __call__(
        self, **kwargs: object
    ) -> tuple[AsyncIterator[bytes], ResourcePack]:
        if self._error is not None:
            raise self._error

        async def _stream() -> AsyncIterator[bytes]:
            yield self._data

        return _stream(), self._pack


def _app(
    *,
    upload: _FakeUseCase | None = None,
    list_: _FakeUseCase | None = None,
    delete: _FakeUseCase | None = None,
    download: _FakeDownloadUseCase | None = None,
    recorder: RecordingAuditRecorder | None = None,
    is_admin: bool = False,
    require_upload_perm: bool = True,
    store: FakeResourcePackStore | None = None,
) -> object:
    app = create_app()
    user = make_user(is_platform_admin=is_admin)
    app.dependency_overrides[get_current_user] = lambda: user
    # The upload gate: if require_upload_perm is True, return the user (passes).
    # If False, raise 403 to simulate "no server:update in any community".
    if require_upload_perm:
        app.dependency_overrides[require_server_update_in_any_community] = lambda: user
    else:
        from fastapi import status

        from mc_server_dashboard_api.http_problem import problem

        async def _deny() -> None:
            raise problem(status.HTTP_403_FORBIDDEN, "forbidden")

        app.dependency_overrides[require_server_update_in_any_community] = _deny

    if upload is not None:
        app.dependency_overrides[get_upload_resource_pack] = lambda: upload
    if list_ is not None:
        app.dependency_overrides[get_list_resource_packs] = lambda: list_
    if delete is not None:
        app.dependency_overrides[get_delete_resource_pack] = lambda: delete
    if download is not None:
        app.dependency_overrides[get_download_resource_pack] = lambda: download
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    if store is not None:
        app.dependency_overrides[get_resource_pack_store] = lambda: store
    return app


class TestUploadEndpoint:
    def test_upload_201(self) -> None:
        p = _pack()
        uc = _FakeUseCase(result=p)
        recorder = RecordingAuditRecorder()
        app = _app(upload=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/resource-packs",
                data={"display_name": "My Pack"},
                files={
                    "file": (
                        "my-pack.zip",
                        io.BytesIO(b"PK\x03\x04"),
                        "application/zip",
                    )
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == str(p.id.value)
        assert body["filename"] == "my-pack.zip"
        assert body["display_name"] == "My Pack"
        # Audit recorded
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == ops.RESOURCE_PACK_UPLOAD
        assert recorder.events[0].outcome == Outcome.SUCCESS

    def test_upload_non_zip_422(self) -> None:
        uc = _FakeUseCase()
        app = _app(upload=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/resource-packs",
                data={"display_name": "Bad"},
                files={"file": ("bad.txt", io.BytesIO(b"data"), "text/plain")},
            )
        assert resp.status_code == 422

    def test_upload_too_large_413(self) -> None:
        uc = _FakeUseCase(error=FileTooLargeError("too big"))
        app = _app(upload=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/resource-packs",
                data={"display_name": "Big"},
                files={
                    "file": (
                        "big.zip",
                        io.BytesIO(b"PK\x03\x04"),
                        "application/zip",
                    )
                },
            )
        assert resp.status_code == 413

    def test_upload_denied_403(self) -> None:
        uc = _FakeUseCase()
        app = _app(upload=uc, require_upload_perm=False)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/resource-packs",
                data={"display_name": "No Perm"},
                files={"file": ("pack.zip", io.BytesIO(b"data"), "application/zip")},
            )
        assert resp.status_code == 403


class TestListEndpoint:
    def test_list_200(self) -> None:
        packs = [
            _pack(filename="a.zip"),
            _pack(filename="b.zip", pack_id=ResourcePackId.new()),
        ]
        uc = _FakeUseCase(result=packs)
        app = _app(list_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/resource-packs")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["resource_packs"]) == 2

    def test_list_empty_200(self) -> None:
        uc = _FakeUseCase(result=[])
        app = _app(list_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/resource-packs")
        assert resp.status_code == 200
        assert resp.json()["resource_packs"] == []


class TestDeleteEndpoint:
    def test_delete_204(self) -> None:
        uc = _FakeUseCase()
        recorder = RecordingAuditRecorder()
        app = _app(delete=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"/api/resource-packs/{_PACK_ID.value}")
        assert resp.status_code == 204
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == ops.RESOURCE_PACK_DELETE

    def test_delete_not_found_404(self) -> None:
        uc = _FakeUseCase(error=ResourcePackNotFoundError("not found"))
        app = _app(delete=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"/api/resource-packs/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_delete_forbidden_403(self) -> None:
        uc = _FakeUseCase(error=PermissionDeniedError("nope"))
        app = _app(delete=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"/api/resource-packs/{uuid.uuid4()}")
        assert resp.status_code == 403

    def test_delete_in_use_409(self) -> None:
        uc = _FakeUseCase(error=ResourcePackInUseError("in use"))
        app = _app(delete=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.delete(f"/api/resource-packs/{uuid.uuid4()}")
        assert resp.status_code == 409


class TestDownloadEndpoint:
    def test_download_200(self) -> None:
        p = _pack()
        uc = _FakeDownloadUseCase(pack=p, data=b"zipbytes")
        recorder = RecordingAuditRecorder()
        app = _app(download=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(f"/api/resource-packs/{p.id.value}/download")
        assert resp.status_code == 200
        assert resp.content == b"zipbytes"
        assert resp.headers["content-type"] == "application/zip"
        assert "attachment" in resp.headers["content-disposition"]
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == ops.RESOURCE_PACK_DOWNLOAD

    def test_download_not_found_404(self) -> None:
        uc = _FakeDownloadUseCase(error=ResourcePackNotFoundError("nope"))
        app = _app(download=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(f"/api/resource-packs/{uuid.uuid4()}/download")
        assert resp.status_code == 404


class TestPublicDownloadEndpoint:
    def test_public_download_200(self) -> None:
        p = _pack(filename="my-pack.zip")
        uc = _FakeDownloadUseCase(pack=p, data=b"publiczip")
        app = _app(download=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(f"/api/public/resource-packs/{p.id.value}/my-pack.zip")
        assert resp.status_code == 200
        assert resp.content == b"publiczip"
        assert resp.headers["content-type"] == "application/zip"

    def test_public_download_wrong_filename_404(self) -> None:
        p = _pack(filename="my-pack.zip")
        uc = _FakeDownloadUseCase(pack=p)
        app = _app(download=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(f"/api/public/resource-packs/{p.id.value}/wrong-name.zip")
        assert resp.status_code == 404

    def test_public_download_not_found_404(self) -> None:
        uc = _FakeDownloadUseCase(error=ResourcePackNotFoundError("nope"))
        app = _app(download=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(f"/api/public/resource-packs/{uuid.uuid4()}/any.zip")
        assert resp.status_code == 404
