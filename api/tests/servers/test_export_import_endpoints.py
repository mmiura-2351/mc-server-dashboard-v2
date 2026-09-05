"""Endpoint tests for the whole-server export / import routes (issue #274).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- the two-layer gate (non-member -> 404, member-without-permission -> 403);
- export is gated by ``file:read`` and streams ``application/zip``; a running
  server is 409 ``server_unsettled`` and audited DENIED;
- the export download grant (issue #2352): the mint's gate and pre-flight,
  redemption without an ``Authorization`` header, and the grant's binding;
- import is gated by ``server:create`` (multipart) and maps the domain errors:
  invalid metadata -> 422, name conflict -> 409, oversized -> 413,
  seed failure -> 503;
- the success audit codes (``server:export`` / ``server:import``).
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
import zipfile
from collections.abc import AsyncIterator

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
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
    get_audit_recorder,
    get_authenticate_download_grant,
    get_authenticate_request,
    get_current_user,
    get_export_server,
    get_import_server,
    get_membership_visibility,
    get_permission_checker,
    get_resolve_server_export,
    get_token_service,
)
from mc_server_dashboard_api.download_cookie import DOWNLOAD_COOKIE_NAME
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.application.authenticate_download_grant import (
    AuthenticateDownloadGrant,
)
from mc_server_dashboard_api.identity.application.authenticate_request import (
    AuthenticateRequest,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.application.export_import import (
    ServerExport,
    export_download_grant_resource,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommunityNotFoundError,
    FileTooLargeError,
    InvalidExportMetadataError,
    InvalidFilePathError,
    ServerFilesUnsettledError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.client_utils import enter_client
from tests.identity.fakes import FakeClock, FakeUnitOfWork, make_user

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)

# A 32-byte HS256 key; the value is irrelevant, only that mint and verify share it.
_SIGNING_KEY = "0123456789abcdef0123456789abcdef"
_GRANT_TTL_SECONDS = 30
_COOKIE_TTL_SECONDS = 900


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


class _FakeExport:
    """Fake :class:`ExportServer`, carrying its exact call signature (#2522)."""

    def __init__(
        self,
        *,
        chunks: list[bytes] | None = None,
        error: Exception | None = None,
        server_name: str = "survival",
    ) -> None:
        self._chunks = chunks or [b"zip-bytes"]
        self._error = error
        self._server_name = server_name
        # The real zip is built inside the returned generator (the adapter's
        # export_dir is lazy), so "the stream was opened" is that body running.
        self.stream_started = False

    async def __call__(
        self, *, community_id: ServersCommunityId, server_id: ServerId
    ) -> ServerExport:
        if self._error is not None:
            raise self._error

        async def _gen() -> AsyncIterator[bytes]:
            self.stream_started = True
            for chunk in self._chunks:
                yield chunk

        return ServerExport(server_name=self._server_name, stream=_gen())


class _FakeResolveExport:
    """Fake :class:`ResolveServerExport` — the grant mint's export pre-flight.

    Carries the real use case's exact call signature (#2522).
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error

    async def __call__(
        self, *, community_id: ServersCommunityId, server_id: ServerId
    ) -> None:
        if self._error is not None:
            raise self._error


class _FakeImport:
    """Fake :class:`ImportServer`, carrying its exact call signature (#2522)."""

    def __init__(
        self, *, result: Server | None = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self, *, community_id: ServersCommunityId, name: str, content: bytes
    ) -> Server:
        self.calls.append(
            {"community_id": community_id, "name": name, "content": content}
        )
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _server_entity(*, community_id: uuid.UUID, name: str = "imported") -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=ServersCommunityId(community_id),
        name=ServerName(name),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        game_port=25565,
    )


def _client(app: object) -> TestClient:
    return enter_client(TestClient(app))  # type: ignore[arg-type]


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


# Set by _app so the export-download tests can mint real access tokens and grants
# against the same signing key and clock the app under test verifies with.
_clock: FakeClock
_tokens: JwtTokenService
_user: User


def _app(
    *,
    member: bool,
    allow: bool,
    subject: User | None = None,
    export: _FakeExport | None = None,
    resolve_export: _FakeResolveExport | None = None,
    import_: _FakeImport | None = None,
    recorder: RecordingAuditRecorder | None = None,
) -> object:
    global _clock, _tokens, _user
    app = _shared_app
    app.dependency_overrides.clear()
    _user = subject if subject is not None else make_user()
    _clock = FakeClock(_NOW)
    _tokens = JwtTokenService(
        signing_key=_SIGNING_KEY,
        algorithm="HS256",
        access_ttl=dt.timedelta(minutes=15),
        download_grant_ttl=dt.timedelta(seconds=_GRANT_TTL_SECONDS),
        download_cookie_ttl=dt.timedelta(seconds=_COOKIE_TTL_SECONDS),
        clock=_clock,
    )
    identity_uow = FakeUnitOfWork()
    identity_uow.users.seed(_user)
    app.dependency_overrides[get_current_user] = lambda: _user
    # The export download resolves its subject itself (Bearer *or* grant), so it
    # goes through these two use cases rather than get_current_user.
    app.dependency_overrides[get_authenticate_request] = lambda: AuthenticateRequest(
        uow=identity_uow, tokens=_tokens
    )
    app.dependency_overrides[get_authenticate_download_grant] = lambda: (
        AuthenticateDownloadGrant(uow=identity_uow, tokens=_tokens)
    )
    app.dependency_overrides[get_token_service] = lambda: _tokens
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if export is not None:
        app.dependency_overrides[get_export_server] = lambda: export
    if resolve_export is not None:
        app.dependency_overrides[get_resolve_server_export] = lambda: resolve_export
    if import_ is not None:
        app.dependency_overrides[get_import_server] = lambda: import_
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return app


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {_tokens.issue_access_token(_user.id)}"}


def _zip_upload() -> tuple[dict[str, tuple[str, bytes, str]], dict[str, str]]:
    """Build the (files, data) pair for the import multipart POST."""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("export_metadata.json", b"{}")
    files = {"file": ("server.zip", buf.getvalue(), "application/zip")}
    data = {"name": "imported"}
    return files, data


# --- export ----------------------------------------------------------------


def _export_url(community: uuid.UUID, server: uuid.UUID) -> str:
    return f"/api/communities/{community}/servers/{server}/export"


def test_non_member_gets_404_on_export() -> None:
    app = _app(member=False, allow=True, export=_FakeExport())
    client = _client(app)
    resp = client.get(_export_url(uuid.uuid4(), uuid.uuid4()), headers=_bearer())
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_export() -> None:
    app = _app(member=True, allow=False, export=_FakeExport())
    client = _client(app)
    resp = client.get(_export_url(uuid.uuid4(), uuid.uuid4()), headers=_bearer())
    assert resp.status_code == 403


def test_export_streams_zip_and_audits() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        export=_FakeExport(chunks=[b"zip", b"-bytes"]),
        recorder=recorder,
    )
    client = _client(app)
    resp = client.get(_export_url(uuid.uuid4(), uuid.uuid4()), headers=_bearer())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.content == b"zip-bytes"
    assert [e.operation for e in recorder.events] == [ops.SERVER_EXPORT]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_SERVER


def test_export_names_the_zip_after_the_server() -> None:
    # A client that navigates the URL rather than being told the filename (a pasted
    # grant link, a CLI fetch) would otherwise save the last path segment, "export".
    app = _app(member=True, allow=True, export=_FakeExport(server_name="survival"))
    client = _client(app)
    resp = client.get(_export_url(uuid.uuid4(), uuid.uuid4()), headers=_bearer())
    assert resp.status_code == 200
    assert (
        resp.headers["content-disposition"]
        == "attachment; filename=\"survival.zip\"; filename*=UTF-8''survival.zip"
    )


def test_export_non_ascii_server_name_uses_the_rfc5987_form() -> None:
    # A server name is free-form, so it can be non-ASCII; the raw name would 500 on
    # the latin-1 header encode, so it rides percent-encoded in filename*.
    app = _app(member=True, allow=True, export=_FakeExport(server_name="サバイバル"))
    client = _client(app)
    resp = client.get(_export_url(uuid.uuid4(), uuid.uuid4()), headers=_bearer())
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    assert 'filename="_____.zip"' in cd  # 5 non-ASCII kana -> 5 underscores
    assert "filename*=UTF-8''%E3%82%B5%E3%83%90%E3%82%A4%E3%83%90%E3%83%AB.zip" in cd


def test_export_server_name_with_path_separators_cannot_traverse() -> None:
    # Only whitespace is trimmed off a server name, so it can hold separators; the
    # saved file must not escape the client's download directory.
    app = _app(member=True, allow=True, export=_FakeExport(server_name="../../etc/pw"))
    client = _client(app)
    resp = client.get(_export_url(uuid.uuid4(), uuid.uuid4()), headers=_bearer())
    assert resp.status_code == 200
    assert (
        resp.headers["content-disposition"]
        == "attachment; filename=\"pw.zip\"; filename*=UTF-8''pw.zip"
    )


def test_export_running_is_409_and_audits_denied() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        export=_FakeExport(error=ServerFilesUnsettledError("x")),
        recorder=recorder,
    )
    client = _client(app)
    resp = client.get(_export_url(uuid.uuid4(), uuid.uuid4()), headers=_bearer())
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"
    assert [e.operation for e in recorder.events] == [ops.SERVER_EXPORT]
    assert recorder.events[0].outcome is Outcome.DENIED


def test_export_without_any_credential_is_401() -> None:
    app = _app(member=True, allow=True, export=_FakeExport())
    client = _client(app)
    resp = client.get(_export_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 401


# --- export download grants (issue #2352) ----------------------------------


def _mint(
    client: TestClient, community: uuid.UUID, server: uuid.UUID
) -> httpx2.Response:
    return client.post(
        f"{_export_url(community, server)}/download-grant", headers=_bearer()
    )


def test_non_member_gets_404_on_export_download_grant() -> None:
    app = _app(member=False, allow=True, resolve_export=_FakeResolveExport())
    client = _client(app)
    assert _mint(client, uuid.uuid4(), uuid.uuid4()).status_code == 404


def test_member_without_permission_gets_403_on_export_download_grant() -> None:
    app = _app(member=True, allow=False, resolve_export=_FakeResolveExport())
    client = _client(app)
    assert _mint(client, uuid.uuid4(), uuid.uuid4()).status_code == 403


def test_export_download_grant_for_unknown_server_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        resolve_export=_FakeResolveExport(error=ServerNotFoundError("x")),
    )
    client = _client(app)
    assert _mint(client, uuid.uuid4(), uuid.uuid4()).status_code == 404


def test_export_download_grant_for_a_running_server_is_409_and_audits_denied() -> None:
    # The common export failure, not a race: without this pre-flight the WebUI
    # would save the problem+json under MyServer.zip (issue #2352).
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        resolve_export=_FakeResolveExport(error=ServerFilesUnsettledError("x")),
        recorder=recorder,
    )
    client = _client(app)
    resp = _mint(client, uuid.uuid4(), uuid.uuid4())
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"
    # The denied row the download would have recorded: minting first must not
    # delete denied-export visibility from the audit log.
    assert [e.operation for e in recorder.events] == [ops.SERVER_EXPORT]
    assert recorder.events[0].outcome is Outcome.DENIED


def test_export_download_grant_response_is_not_cached_and_reports_expiry() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, resolve_export=_FakeResolveExport())
    client = _client(app)
    resp = _mint(client, community, server)
    assert resp.status_code == 200
    # A URL that carries a credential must never sit in a shared cache.
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["download_url"].startswith(f"{_export_url(community, server)}?grant=")
    assert body["expires_at"] == (
        _NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS)
    ).isoformat().replace("+00:00", "Z")


def test_minting_an_export_grant_records_no_audit_event() -> None:
    # Bytes leave the system at redemption, not at issuance (issue #2352).
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True, allow=True, resolve_export=_FakeResolveExport(), recorder=recorder
    )
    client = _client(app)
    assert _mint(client, uuid.uuid4(), uuid.uuid4()).status_code == 200
    assert recorder.events == []


def test_minted_export_url_downloads_without_an_authorization_header() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        export=_FakeExport(chunks=[b"zip-bytes"]),
        resolve_export=_FakeResolveExport(),
        recorder=recorder,
    )
    client = _client(app)
    url = _mint(client, community, server).json()["download_url"]

    resp = client.get(url)

    assert resp.status_code == 200
    assert resp.content == b"zip-bytes"
    assert resp.headers["content-type"] == "application/zip"
    # Exactly one audit row, at redemption, with the grant's subject as actor.
    assert [e.operation for e in recorder.events] == [ops.SERVER_EXPORT]
    assert recorder.events[0].actor_id == _user.id.value


def _export_grant_url(community: uuid.UUID, server: uuid.UUID, *, subject: User) -> str:
    issued = _tokens.issue_download_grant(
        subject.id, export_download_grant_resource(community, server)
    )
    return f"{_export_url(community, server)}?grant={issued.token}"


def test_export_grant_redeemed_download_matches_the_bearer_response() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, export=_FakeExport(chunks=[b"zip-bytes"]))
    client = _client(app)

    with_bearer = client.get(_export_url(community, server), headers=_bearer())
    with_grant = client.get(_export_grant_url(community, server, subject=_user))

    assert with_grant.status_code == with_bearer.status_code == 200
    assert with_grant.content == with_bearer.content
    assert with_grant.headers["content-type"] == with_bearer.headers["content-type"]


def test_export_grant_is_rejected_after_its_ttl() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, export=_FakeExport())
    client = _client(app)
    url = _export_grant_url(community, server, subject=_user)

    _clock.set(_NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS))

    assert client.get(url).status_code == 401


def test_export_grant_is_rejected_under_another_server_or_community() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, export=_FakeExport())
    client = _client(app)
    issued = _tokens.issue_download_grant(
        _user.id, export_download_grant_resource(community, server)
    )

    other_server = client.get(
        f"{_export_url(community, uuid.uuid4())}?grant={issued.token}"
    )
    other_community = client.get(
        f"{_export_url(uuid.uuid4(), server)}?grant={issued.token}"
    )

    assert other_server.status_code == 401
    assert other_community.status_code == 401


def test_export_grant_is_not_accepted_as_a_bearer_token() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, export=_FakeExport())
    client = _client(app)
    issued = _tokens.issue_download_grant(
        _user.id, export_download_grant_resource(community, server)
    )

    resp = client.get(
        _export_url(community, server),
        headers={"Authorization": f"Bearer {issued.token}"},
    )

    assert resp.status_code == 401


def test_access_token_is_not_accepted_as_an_export_grant() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, export=_FakeExport())
    client = _client(app)
    access = _tokens.issue_access_token(_user.id)

    resp = client.get(f"{_export_url(community, server)}?grant={access}")

    assert resp.status_code == 401


def test_export_grant_loses_to_a_permission_revoked_after_issuance() -> None:
    # The grant proves identity, never authority: authorization is decided afresh.
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=False, export=_FakeExport())
    client = _client(app)

    resp = client.get(_export_grant_url(community, server, subject=_user))

    assert resp.status_code == 403


def test_export_grant_loses_to_a_membership_removed_after_issuance() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=False, allow=True, export=_FakeExport())
    client = _client(app)

    resp = client.get(_export_grant_url(community, server, subject=_user))

    assert resp.status_code == 404


def test_export_grant_for_a_deactivated_subject_is_rejected() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=True, allow=True, subject=make_user(active=False), export=_FakeExport()
    )
    client = _client(app)

    resp = client.get(_export_grant_url(community, server, subject=_user))

    assert resp.status_code == 401


# --- the export download cookie (issue #2373) -------------------------------
#
# The export shares one mechanism with the backup and file downloads
# (``require_download_access`` + ``download_cookie``), which is where the property
# matrix is pinned (test_backup_endpoints.py). What is per-route here is the Path
# scope and that the retry of *this* URL resumes.


def _export_cookie_header(community: uuid.UUID, server: uuid.UUID) -> dict[str, str]:
    value = _tokens.issue_download_cookie(
        _user.id, export_download_grant_resource(community, server)
    )
    return {"Cookie": f"{DOWNLOAD_COOKIE_NAME}={value}"}


def test_export_grant_redemption_sets_a_path_scoped_cookie() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, export=_FakeExport(chunks=[b"zip-bytes"]))
    client = _client(app)

    resp = client.get(_export_grant_url(community, server, subject=_user))

    assert resp.status_code == 200
    cookie = next(
        h
        for h in resp.headers.get_list("set-cookie")
        if h.startswith(f"{DOWNLOAD_COOKIE_NAME}=")
    )
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert f"Path={_export_url(community, server)}" in cookie
    # RFC 6265 Section 3 leaves a Set-Cookie response cacheable, so without this
    # header a shared cache could replay the credential to a second client.
    assert resp.headers["cache-control"] == "no-store"


def test_expired_export_grant_is_retried_with_the_cookie() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, export=_FakeExport(chunks=[b"zip-bytes"]))
    client = _client(app)
    url = _export_grant_url(community, server, subject=_user)

    _clock.set(_NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS))

    assert client.get(url).status_code == 401
    resumed = client.get(url, headers=_export_cookie_header(community, server))
    assert resumed.status_code == 200
    assert resumed.content == b"zip-bytes"


def test_export_cookie_is_rejected_under_another_server_or_community() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, export=_FakeExport())
    client = _client(app)
    headers = _export_cookie_header(community, server)

    other_server = client.get(_export_url(community, uuid.uuid4()), headers=headers)
    other_community = client.get(_export_url(uuid.uuid4(), server), headers=headers)

    assert other_server.status_code == 401
    assert other_community.status_code == 401


def test_an_unsettled_export_mints_no_cookie() -> None:
    # The 409 the export's precondition raises: no credential for a ZIP that was
    # never served.
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=True,
        allow=True,
        export=_FakeExport(error=ServerFilesUnsettledError("x")),
    )
    client = _client(app)

    resp = client.get(_export_grant_url(community, server, subject=_user))

    assert resp.status_code == 409
    assert not any(
        h.startswith(f"{DOWNLOAD_COOKIE_NAME}=")
        for h in resp.headers.get_list("set-cookie")
    )


# --- Cache-Control on the served export (issue #2491) ----------------------


def test_export_download_declares_no_store_under_every_credential() -> None:
    # The header belongs to the response being a per-user body, not to the
    # credential that fetched it. A cookie-authenticated request in particular
    # carries no Authorization, so RFC 9111 Section 3.5's default protection from
    # shared caches does not cover it.
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, export=_FakeExport(chunks=[b"zip-bytes"]))
    client = _client(app)

    with_bearer = client.get(_export_url(community, server), headers=_bearer())
    with_cookie = client.get(
        _export_url(community, server),
        headers=_export_cookie_header(community, server),
    )
    # Last, because redeeming a grant mints the cookie into the client's jar.
    with_grant = client.get(_export_grant_url(community, server, subject=_user))

    for resp in (with_bearer, with_cookie, with_grant):
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"


# --- the HEAD probe (issue #2383) ------------------------------------------
#
# A download client asks HEAD first, to learn what the transfer would be before
# starting it. Per credential, like the Cache-Control section above: the gate is
# what a future auth change could silently drop a route from.


def test_export_head_answers_the_gets_headers_under_every_credential() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, export=_FakeExport(chunks=[b"zip-bytes"]))
    client = _client(app)
    url = _export_url(community, server)
    cookie = _export_cookie_header(community, server)
    # Last, because redeeming a grant mints the cookie into the client's jar.
    grant_url = _export_grant_url(community, server, subject=_user)

    probes = [
        client.head(url, headers=_bearer()),
        client.head(url, headers=cookie),
        client.head(grant_url),
    ]
    served = client.get(url, headers=_bearer())

    for resp in probes:
        assert resp.status_code == served.status_code == 200
        # A HEAD carries the GET's headers and none of its bytes.
        assert resp.content == b""
        for name in ("content-type", "content-disposition", "cache-control"):
            assert resp.headers[name] == served.headers[name], name
        # The zip is built incrementally, so the GET declares no length; a probe
        # that answered "0" would tell the client the export is empty.
        assert "content-length" not in resp.headers


def test_export_head_neither_builds_the_zip_nor_records_an_export() -> None:
    # Returning the right headers is easy; doing it without walking the working
    # set is the point of the probe (issue #2383). And a probe is not an export,
    # so it records nothing: an audited HEAD would inflate the server:export count.
    community, server = uuid.uuid4(), uuid.uuid4()
    export = _FakeExport(chunks=[b"zip-bytes"])
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, export=export, recorder=recorder)
    client = _client(app)

    resp = client.head(_export_url(community, server), headers=_bearer())

    assert resp.status_code == 200
    assert export.stream_started is False
    assert recorder.events == []


def test_export_head_of_a_running_server_is_409_and_records_nothing() -> None:
    # The GET records server:export DENIED here; the probe does not, for the same
    # reason it does not record a success — it is not an export attempt.
    community, server = uuid.uuid4(), uuid.uuid4()
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        export=_FakeExport(error=ServerFilesUnsettledError("x")),
        recorder=recorder,
    )
    client = _client(app)

    resp = client.head(_export_url(community, server), headers=_bearer())

    assert resp.status_code == 409
    assert recorder.events == []


def _export_credential(
    kind: str, community: uuid.UUID, server: uuid.UUID
) -> tuple[str, dict[str, str]]:
    """The export URL and headers for one of the three credential transports."""

    if kind == "bearer":
        return _export_url(community, server), _bearer()
    if kind == "cookie":
        return _export_url(community, server), _export_cookie_header(community, server)
    return _export_grant_url(community, server, subject=_user), {}


@pytest.mark.parametrize("kind", ["bearer", "cookie", "grant"])
@pytest.mark.parametrize(
    ("member", "allow", "error", "expected"),
    [
        (True, True, None, 200),
        (False, True, None, 404),
        (True, False, None, 403),
        (True, True, ServerNotFoundError("x"), 404),
        (True, True, ServerFilesUnsettledError("x"), 409),
    ],
)
def test_export_head_is_answered_exactly_like_the_get(
    kind: str, member: bool, allow: bool, error: Exception | None, expected: int
) -> None:
    # A HEAD that skipped or weakened a check would be a security defect, not a
    # convenience gap. Every credential must be refused on the probe exactly
    # where it is refused on the export.
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=member, allow=allow, export=_FakeExport(error=error))
    url, headers = _export_credential(kind, community, server)

    probed = _client(app).head(url, headers=headers)
    served = _client(app).get(url, headers=headers)

    assert probed.status_code == served.status_code == expected


def test_export_head_without_credentials_is_401() -> None:
    app = _app(member=True, allow=True, export=_FakeExport())
    client = _client(app)

    resp = client.head(_export_url(uuid.uuid4(), uuid.uuid4()))

    assert resp.status_code == 401


# --- import ----------------------------------------------------------------


def test_non_member_gets_404_on_import() -> None:
    app = _app(member=False, allow=True, import_=_FakeImport())
    client = _client(app)
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_import() -> None:
    app = _app(member=True, allow=False, import_=_FakeImport())
    client = _client(app)
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 403


def test_import_creates_server_and_audits() -> None:
    community = uuid.uuid4()
    recorder = RecordingAuditRecorder()
    use_case = _FakeImport(result=_server_entity(community_id=community))
    app = _app(member=True, allow=True, import_=use_case, recorder=recorder)
    client = _client(app)
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{community}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "imported"
    assert body["game_port"] == 25565
    # The name comes from the request form, not the metadata.
    assert use_case.calls[0]["name"] == "imported"
    assert [e.operation for e in recorder.events] == [ops.SERVER_IMPORT]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_SERVER


def test_import_invalid_metadata_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(error=InvalidExportMetadataError("bad")),
    )
    client = _client(app)
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_export_metadata"


def test_import_name_conflict_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(error=ServerNameAlreadyExistsError("imported")),
    )
    client = _client(app)
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_name_exists"


def test_import_into_a_concurrently_deleted_community_is_404() -> None:
    # issue #2940: import composes CreateServer, so it stages the same server row
    # and reaches the same commit-time fk_server_community_id_community. A racer
    # deleting the community between the authorization gate's membership read and
    # that commit gets the 404 the gate itself raises a moment earlier.
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(
            error=CommunityNotFoundError("fk_server_community_id_community")
        ),
    )
    client = _client(app)
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 404
    assert resp.json()["reason"] == "not_found"


def test_import_oversized_is_413() -> None:
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(error=FileTooLargeError("9999")),
    )
    client = _client(app)
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 413


def test_import_seed_failure_is_503() -> None:
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(error=WorkingSetSeedFailedError("x")),
    )
    client = _client(app)
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 503
    assert resp.json()["reason"] == "seed_failed"


# --- the archive's path rejections reach the wire (issue #2869) -------------
#
# The route carries the reason the use case raised, so the Web UI's switch on
# ``reason`` sees the SAME contract import gives as the five files-API doors.


def test_import_member_under_root_properties_is_422_platform_managed_path() -> None:
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(
            error=InvalidFilePathError(
                "server.properties/notes.txt", reason="platform_managed_path"
            )
        ),
    )
    client = _client(app)
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "platform_managed_path"


def test_import_zip_slip_member_is_422_invalid_path() -> None:
    # The same handler carries the default reason, so the zip-slip rejection the
    # docstring has always promised finally answers 422 instead of an unmapped
    # 500.
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(error=InvalidFilePathError("../escape.txt")),
    )
    client = _client(app)
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_path"
