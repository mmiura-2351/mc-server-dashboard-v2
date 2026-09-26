"""Reusable contracts for the identity persistence Ports (#3135)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from mc_server_dashboard_api.identity.domain.entities import RefreshToken, User
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    UsernameAlreadyExistsError,
)
from mc_server_dashboard_api.identity.domain.repositories import (
    RefreshTokenRepository,
    UserRepository,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    RefreshTokenId,
    UserId,
    Username,
)
from tests.contracts.repository import RepositoryHarness

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.UTC)


@dataclass(frozen=True)
class RefreshTokenRepositoryHarness(RepositoryHarness[RefreshTokenRepository]):
    user_id: UserId
    other_user_id: UserId


def _user(username: str = "alice", email: str = "alice@example.com") -> User:
    return User(
        id=UserId.new(),
        username=Username(username),
        email=EmailAddress(email),
        password_hash="hash",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _token(
    harness: RefreshTokenRepositoryHarness,
    token_hash: str = "hash-1",
    *,
    user_id: UserId | None = None,
) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user_id or harness.user_id,
        token_hash=token_hash,
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=30),
    )


class UserRepositoryContract:
    """Observable contract promised by :class:`UserRepository`."""

    async def test_add_detaches_the_persisted_values(
        self, user_repository_harness: RepositoryHarness[UserRepository]
    ) -> None:
        user = _user()
        async with user_repository_harness.open() as transaction:
            await transaction.repository.add(user)
            user.username = Username("rewritten-before-commit")
            user.active = False
            await transaction.commit()

        async with user_repository_harness.open() as transaction:
            loaded = await transaction.repository.get_by_id(user.id)

        assert loaded is not None
        assert loaded.username == Username("alice")
        assert loaded.active is True

    async def test_readers_return_detached_entities_and_none_for_missing_rows(
        self, user_repository_harness: RepositoryHarness[UserRepository]
    ) -> None:
        user = _user(username="Alice")
        async with user_repository_harness.open() as transaction:
            await transaction.repository.add(user)
            await transaction.commit()

        async with user_repository_harness.open() as transaction:
            by_id = await transaction.repository.get_by_id(user.id)
            by_username = await transaction.repository.get_by_username(
                Username("alice")
            )
            by_email = await transaction.repository.get_by_email(user.email)
            listed = await transaction.repository.list_page(limit=10, offset=0)
            assert by_id is not None
            assert by_username is not None
            assert by_email is not None
            assert [row.id for row in listed] == [user.id]
            for handed_out in (by_id, by_username, by_email, listed[0]):
                handed_out.email = EmailAddress("rewritten@example.com")
                handed_out.active = False

        async with user_repository_harness.open() as transaction:
            reloaded = await transaction.repository.get_by_id(user.id)
            missing_id = await transaction.repository.get_by_id(UserId.new())
            missing_username = await transaction.repository.get_by_username(
                Username("missing")
            )
            missing_email = await transaction.repository.get_by_email(
                EmailAddress("missing@example.com")
            )

        assert reloaded is not None
        assert reloaded.email == EmailAddress("alice@example.com")
        assert reloaded.active is True
        assert missing_id is None
        assert missing_username is None
        assert missing_email is None

    async def test_update_detaches_the_persisted_values(
        self, user_repository_harness: RepositoryHarness[UserRepository]
    ) -> None:
        user = _user()
        async with user_repository_harness.open() as transaction:
            await transaction.repository.add(user)
            await transaction.commit()

        user.username = Username("alice-renamed")
        user.email = EmailAddress("renamed@example.com")
        user.password_hash = "rotated-hash"
        user.updated_at = _NOW + dt.timedelta(hours=1)
        user.created_at = _NOW + dt.timedelta(days=1)
        async with user_repository_harness.open() as transaction:
            await transaction.repository.update(user)
            user.password_hash = "rewritten-after-update"
            user.active = False
            await transaction.commit()

        async with user_repository_harness.open() as transaction:
            loaded = await transaction.repository.get_by_id(user.id)

        assert loaded is not None
        assert loaded.username == Username("alice-renamed")
        assert loaded.email == EmailAddress("renamed@example.com")
        assert loaded.password_hash == "rotated-hash"
        assert loaded.active is True
        assert loaded.updated_at == _NOW + dt.timedelta(hours=1)
        assert loaded.created_at == _NOW

    async def test_update_of_missing_row_is_a_no_op(
        self, user_repository_harness: RepositoryHarness[UserRepository]
    ) -> None:
        user = _user()
        async with user_repository_harness.open() as transaction:
            await transaction.repository.update(user)
            await transaction.commit()

        async with user_repository_harness.open() as transaction:
            assert await transaction.repository.get_by_id(user.id) is None

    async def test_username_and_email_are_unique_for_add_and_update(
        self, user_repository_harness: RepositoryHarness[UserRepository]
    ) -> None:
        alice = _user(username="Alice", email="alice@example.com")
        bob = _user(username="bob", email="bob@example.com")
        async with user_repository_harness.open() as transaction:
            await transaction.repository.add(alice)
            await transaction.repository.add(bob)
            await transaction.commit()

        with pytest.raises(UsernameAlreadyExistsError):
            async with user_repository_harness.open() as transaction:
                await transaction.repository.add(
                    _user(username="ALICE", email="other@example.com")
                )
                await transaction.commit()

        with pytest.raises(EmailAlreadyExistsError):
            async with user_repository_harness.open() as transaction:
                await transaction.repository.add(
                    _user(username="other", email="ALICE@EXAMPLE.COM")
                )
                await transaction.commit()

        bob.username = Username("alice")
        with pytest.raises(UsernameAlreadyExistsError):
            async with user_repository_harness.open() as transaction:
                await transaction.repository.update(bob)
                await transaction.commit()

        bob.username = Username("bob")
        bob.email = EmailAddress("alice@example.com")
        with pytest.raises(EmailAlreadyExistsError):
            async with user_repository_harness.open() as transaction:
                await transaction.repository.update(bob)
                await transaction.commit()


class RefreshTokenRepositoryContract:
    """Observable contract promised by :class:`RefreshTokenRepository`."""

    async def test_add_detaches_the_persisted_values(
        self, refresh_token_repository_harness: RefreshTokenRepositoryHarness
    ) -> None:
        token = _token(refresh_token_repository_harness)
        expected_expiry = token.expires_at
        async with refresh_token_repository_harness.open() as transaction:
            await transaction.repository.add(token)
            token.expires_at = _NOW
            token.revoked_at = _NOW
            await transaction.commit()

        async with refresh_token_repository_harness.open() as transaction:
            loaded = await transaction.repository.get_by_token_hash("hash-1")

        assert loaded is not None
        assert loaded.expires_at == expected_expiry
        assert loaded.revoked_at is None

    async def test_readers_return_detached_entities_and_scope_active_sessions(
        self, refresh_token_repository_harness: RefreshTokenRepositoryHarness
    ) -> None:
        target = _token(refresh_token_repository_harness)
        other = _token(
            refresh_token_repository_harness,
            "hash-2",
            user_id=refresh_token_repository_harness.other_user_id,
        )
        async with refresh_token_repository_harness.open() as transaction:
            await transaction.repository.add(target)
            await transaction.repository.add(other)
            await transaction.commit()

        async with refresh_token_repository_harness.open() as transaction:
            by_hash = await transaction.repository.get_by_token_hash(target.token_hash)
            listed = await transaction.repository.list_active_for_user(
                target.user_id, now=_NOW
            )
            assert by_hash is not None
            assert [row.id for row in listed] == [target.id]
            by_hash.revoked_at = _NOW
            listed[0].expires_at = _NOW

        async with refresh_token_repository_harness.open() as transaction:
            reloaded = await transaction.repository.get_by_token_hash(target.token_hash)
            relisted = await transaction.repository.list_active_for_user(
                target.user_id, now=_NOW
            )
            missing = await transaction.repository.get_by_token_hash("missing")

        assert reloaded is not None
        assert reloaded.revoked_at is None
        assert [row.id for row in relisted] == [target.id]
        assert relisted[0].expires_at == _NOW + dt.timedelta(days=30)
        assert missing is None
