"""Tests for the identity-backed backup author directory adapter (issue #688).

Verifies the adapter translates raw author UUIDs to display usernames over the
identity user store: a known author resolves, an unknown author (deleted user) is
omitted so the caller falls back to the raw id, and an empty input never touches
the store.
"""

from __future__ import annotations

import uuid

from mc_server_dashboard_api.servers.adapters.backup_author_directory import (
    IdentityBackupAuthorDirectory,
)
from tests.identity.fakes import FakeUnitOfWork, make_user


async def test_resolves_known_authors_to_usernames() -> None:
    alice = make_user(username="alice", email="alice@example.com")
    bob = make_user(username="bob", email="bob@example.com")
    uow = FakeUnitOfWork()
    uow.users.seed(alice)
    uow.users.seed(bob)
    directory = IdentityBackupAuthorDirectory(uow)

    resolved = await directory.usernames_for([alice.id.value, bob.id.value])

    assert resolved == {alice.id.value: "alice", bob.id.value: "bob"}


async def test_omits_unknown_author() -> None:
    alice = make_user(username="alice", email="alice@example.com")
    uow = FakeUnitOfWork()
    uow.users.seed(alice)
    directory = IdentityBackupAuthorDirectory(uow)
    deleted = uuid.uuid4()

    resolved = await directory.usernames_for([alice.id.value, deleted])

    assert resolved == {alice.id.value: "alice"}


async def test_empty_input_returns_empty() -> None:
    directory = IdentityBackupAuthorDirectory(FakeUnitOfWork())

    assert await directory.usernames_for([]) == {}
