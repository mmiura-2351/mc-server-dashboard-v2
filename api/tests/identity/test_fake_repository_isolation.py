"""Entity isolation across the identity repository fakes (#2516).

The servers context established the rule in
:mod:`tests.servers.test_server_repository_fake` (#2505, PR #2512); the identity
fakes carry the same defect and get the same answer. Restated: a real adapter is
a one-way seam in both directions -- a writer serializes the entity into an
INSERT/UPDATE, so no later in-memory edit can reach the row, and a reader
materializes a fresh entity per SELECT, so an edit to a loaded one reaches the
database only through a writer.

A fake that stores, or hands back, the caller's object is therefore *more
forgiving than production*, and only in that direction: a mutant that should
redden a persisted-state assertion can be absorbed by the aliasing with nothing
visible in the test source. ``test_admin_create_user`` had made that aliasing
load-bearing (``assert uow.users.by_id[user.id] is user``), so the fake could
not be corrected without correcting the assertion.

Detachment is one half of a writer's fidelity; whether the row EXISTS at all is
the other (#2557, following PR #2556). An ``UPDATE ... WHERE id = :id`` matches
nothing on an absent id, so nothing is written and no row appears -- the fake
must not conjure one, or a test can assert a state production never reaches.

``User`` and ``RefreshToken`` carry no mutable field -- every one is a scalar, a
datetime, or a frozen value object -- so a new entity is the full copy depth.
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    RefreshTokenId,
    UserId,
    Username,
)
from tests.identity.fakes import (
    FakeRefreshTokenRepository,
    FakeUserRepository,
    make_user,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.UTC)


def test_user_seed_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeUserRepository()
    user = make_user()

    repo.seed(user)

    stored = repo.by_id[user.id]
    user.username = Username("rewritten")
    user.is_platform_admin = True
    assert stored.username == Username("alice")
    assert stored.is_platform_admin is False


async def test_user_add_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeUserRepository()
    user = make_user()

    await repo.add(user)

    stored = repo.by_id[user.id]
    user.active = False
    assert stored.active is True


async def test_user_update_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeUserRepository()
    user = make_user()
    repo.seed(user)
    user.password_hash = "hashed::rotated"

    await repo.update(user)

    stored = repo.by_id[user.id]
    user.password_hash = "hashed::after-the-write"
    assert stored.password_hash == "hashed::rotated"


async def test_user_update_on_a_missing_row_is_a_no_op() -> None:
    # ``SqlAlchemyUserRepository.update`` issues
    # ``UPDATE "user" SET ... WHERE id = :id``
    # (identity/adapters/repositories.py:117-138), which matches no row on an
    # absent id: nothing is written, nothing is raised, and the result of
    # ``session.execute`` is discarded under a ``-> None`` signature, so no
    # caller can read a rows-affected signal either. Its one error path --
    # translating the IntegrityError a rename into a taken username/email
    # raises -- needs a matching row to fire at all (#2557).
    repo = FakeUserRepository()
    user = make_user()

    await repo.update(user)

    assert repo.by_id == {}


async def test_user_readers_hand_out_copies() -> None:
    repo = FakeUserRepository()
    user = make_user()
    repo.seed(user)

    loaded = [
        await repo.get_by_id(user.id),
        await repo.get_by_username(user.username),
        await repo.get_by_email(user.email),
        (await repo.list_page(limit=10, offset=0))[0],
    ]

    for handed_out in loaded:
        assert handed_out is not None
        handed_out.email = EmailAddress("rewritten@example.com")
        handed_out.active = False
    stored = repo.by_id[user.id]
    assert stored.email == EmailAddress("alice@example.com")
    assert stored.active is True


def _token(user_id: UserId) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user_id,
        token_hash="hash-1",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=30),
    )


async def test_refresh_token_writers_store_copies_the_caller_cannot_rewrite() -> None:
    repo = FakeRefreshTokenRepository()
    user = make_user()
    seeded = _token(user.id)
    added = _token(user.id)
    added.token_hash = "hash-2"

    repo.seed(seeded)
    await repo.add(added)

    seeded.revoked_at = _NOW
    added.expires_at = _NOW
    assert repo.by_hash["hash-1"].revoked_at is None
    assert repo.by_hash["hash-2"].expires_at == _NOW + dt.timedelta(days=30)


async def test_refresh_token_readers_hand_out_copies() -> None:
    repo = FakeRefreshTokenRepository()
    user = make_user()
    repo.seed(_token(user.id))

    loaded = await repo.get_by_token_hash("hash-1")
    (listed,) = await repo.list_active_for_user(user.id, now=_NOW)

    assert loaded is not None
    loaded.revoked_at = _NOW
    listed.revoked_reason = "rewritten"
    stored = repo.by_hash["hash-1"]
    assert stored.revoked_at is None
    assert stored.revoked_reason is None
