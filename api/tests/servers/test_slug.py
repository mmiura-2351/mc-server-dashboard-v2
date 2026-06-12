"""Tests for server slug: generation, validation, create auto-assign, rename (#955).

Covers:
- Charset and length rules (:func:`validate_slug`).
- Reserved-word rejection.
- Generation: correct format, draws from wordlist, retry on collision.
- :class:`SlugExhaustedError` when all retries taken.
- :class:`CreateServer` auto-assigns a unique slug.
- :class:`UpdateServer` slug rename: happy path, 422 invalid, 409 taken, permission.
- Backfill: migration helper is statically testable via the slug format.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from unittest.mock import patch

import pytest

from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    UpdateServer,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidSlugError,
    PermissionDeniedError,
    SlugAlreadyTakenError,
    SlugExhaustedError,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.slug import (
    _RESERVED,
    _WORDS_A,
    _WORDS_B,
    generate_slug,
    validate_slug,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeServerRepository,
    FakeUnitOfWork,
    FakeVersionValidator,
)

_COMMUNITY = CommunityId(uuid.uuid4())
_PORTS = PortRange(start=25565, end=25664)
_NOW = dt.datetime(2026, 6, 12, 12, 0, tzinfo=dt.timezone.utc)

_SLUG_PATTERN = re.compile(r"^[a-z]+-[a-z]+-\d{2}$")


# ---------------------------------------------------------------------------
# validate_slug
# ---------------------------------------------------------------------------


def test_validate_slug_accepts_valid_dns_label() -> None:
    validate_slug("amber-falcon-42")  # no exception


def test_validate_slug_accepts_single_char_alphanumeric() -> None:
    validate_slug("a")
    validate_slug("0")


def test_validate_slug_accepts_63_char_label() -> None:
    validate_slug("a" * 63)


def test_validate_slug_rejects_64_chars() -> None:
    with pytest.raises(InvalidSlugError):
        validate_slug("a" * 64)


def test_validate_slug_rejects_leading_hyphen() -> None:
    with pytest.raises(InvalidSlugError):
        validate_slug("-amber")


def test_validate_slug_rejects_trailing_hyphen() -> None:
    with pytest.raises(InvalidSlugError):
        validate_slug("amber-")


def test_validate_slug_rejects_uppercase() -> None:
    with pytest.raises(InvalidSlugError):
        validate_slug("Amber-falcon-42")


def test_validate_slug_rejects_spaces() -> None:
    with pytest.raises(InvalidSlugError):
        validate_slug("amber falcon")


def test_validate_slug_rejects_empty() -> None:
    with pytest.raises(InvalidSlugError):
        validate_slug("")


def test_validate_slug_rejects_reserved_words() -> None:
    for word in _RESERVED:
        with pytest.raises(InvalidSlugError):
            validate_slug(word)


def test_validate_slug_reserved_list_is_not_empty() -> None:
    assert len(_RESERVED) > 0
    assert "api" in _RESERVED
    assert "relay" in _RESERVED
    assert "www" in _RESERVED


# ---------------------------------------------------------------------------
# generate_slug
# ---------------------------------------------------------------------------


def test_generate_slug_matches_pattern() -> None:
    slug = generate_slug(taken=set())
    assert _SLUG_PATTERN.match(slug), f"unexpected slug format: {slug!r}"


def test_generate_slug_uses_wordlist_parts() -> None:
    """The first and second parts must come from the embedded word lists."""
    slug = generate_slug(taken=set())
    parts = slug.rsplit("-", maxsplit=1)
    assert len(parts) == 2
    # Second part must be two digits.
    assert re.fullmatch(r"\d{2}", parts[1]), parts[1]
    # Rejoin the prefix and split on first hyphen.
    prefix = parts[0]
    split = prefix.split("-", 1)
    assert len(split) == 2, f"expected two words in prefix: {prefix!r}"
    word_a, word_b = split
    assert word_a in _WORDS_A, f"{word_a!r} not in _WORDS_A"
    assert word_b in _WORDS_B, f"{word_b!r} not in _WORDS_B"


def test_generate_slug_is_not_in_taken() -> None:
    taken: set[str] = set()
    for _ in range(20):
        slug = generate_slug(taken=taken)
        assert slug not in taken
        taken.add(slug)


def test_generate_slug_retries_on_collision() -> None:
    """If the first candidate is taken, a different slug is returned."""
    # The generator calls random.choice twice (once per word list) and
    # random.randint once per attempt. Pin choice to always return the first
    # element of whichever sequence it receives; pin randint to return 42 on the
    # first attempt and 43 on the second.
    first = f"{_WORDS_A[0]}-{_WORDS_B[0]}-42"
    second = f"{_WORDS_A[0]}-{_WORDS_B[0]}-43"
    taken = {first}
    randint_calls: list[int] = []

    def _fixed_choice(seq: tuple[str, ...]) -> str:
        return seq[0]

    def _fixed_randint(_a: int, _b: int) -> int:
        call_num = len(randint_calls)
        val = 42 if call_num == 0 else 43
        randint_calls.append(val)
        return val

    _choice_path = "mc_server_dashboard_api.servers.domain.slug.random.choice"
    _randint_path = "mc_server_dashboard_api.servers.domain.slug.random.randint"
    with (
        patch(_choice_path, _fixed_choice),
        patch(_randint_path, _fixed_randint),
    ):
        slug = generate_slug(taken=taken)

    assert slug == second
    assert len(randint_calls) == 2


def test_generate_slug_raises_when_all_attempts_taken() -> None:
    """SlugExhaustedError when every generated candidate is taken."""
    # Build a taken set that matches the slug produced by pinned randomness so
    # every attempt collides and the exhaustion guard fires.
    slug = f"{_WORDS_A[0]}-{_WORDS_B[0]}-00"
    taken = {slug}
    with (
        patch(
            "mc_server_dashboard_api.servers.domain.slug.random.choice",
            lambda seq: seq[0],
        ),
        patch(
            "mc_server_dashboard_api.servers.domain.slug.random.randint",
            lambda _a, _b: 0,
        ),
    ):
        with pytest.raises(SlugExhaustedError):
            generate_slug(taken=taken)


def test_generate_slug_result_passes_validation() -> None:
    """Any generated slug must be valid per validate_slug."""
    for _ in range(50):
        slug = generate_slug(taken=set())
        validate_slug(slug)


# ---------------------------------------------------------------------------
# CreateServer auto-assigns a slug
# ---------------------------------------------------------------------------


def _make_create_use_case() -> CreateServer:
    return CreateServer(
        uow=FakeUnitOfWork(),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=_PORTS,
    )


async def test_create_server_assigns_slug() -> None:
    use_case = _make_create_use_case()
    server = await use_case(
        community_id=_COMMUNITY,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    assert server.slug
    validate_slug(server.slug)  # must be a valid DNS label


async def test_create_server_slug_is_unique_across_creates() -> None:
    """Two creates in the same UoW (shared repo) get different slugs."""
    uow = FakeUnitOfWork()
    use_case = CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=_PORTS,
    )
    s1 = await use_case(
        community_id=_COMMUNITY,
        name="s1",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    s2 = await use_case(
        community_id=_COMMUNITY,
        name="s2",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    assert s1.slug != s2.slug


# ---------------------------------------------------------------------------
# UpdateServer slug rename
# ---------------------------------------------------------------------------


def _server(
    *,
    slug: str = "amber-falcon-42",
    desired: DesiredState = DesiredState.STOPPED,
    observed: ObservedState = ObservedState.STOPPED,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=_COMMUNITY,
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        game_port=25565,
        slug=slug,
        desired_state=desired,
        observed_state=observed,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_update_use_case(repo: FakeServerRepository) -> UpdateServer:
    uow = FakeUnitOfWork(servers=repo)
    return UpdateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        file_store=FakeFileStore(),
        port_range=_PORTS,
    )


async def _authorize_allow(perm: str) -> bool:
    return True


async def _authorize_deny(perm: str) -> bool:
    return False


async def test_rename_slug_happy_path() -> None:
    server = _server(slug="amber-falcon-42")
    repo = FakeServerRepository()
    repo.seed(server)
    use_case = _make_update_use_case(repo)

    updated = await use_case(
        community_id=_COMMUNITY,
        server_id=server.id,
        slug="cedar-wolf-07",
        authorize=_authorize_allow,
    )
    assert updated.slug == "cedar-wolf-07"


async def test_rename_slug_same_slug_is_noop() -> None:
    """Renaming a slug to its current value is not a conflict."""
    server = _server(slug="amber-falcon-42")
    repo = FakeServerRepository()
    repo.seed(server)
    use_case = _make_update_use_case(repo)

    updated = await use_case(
        community_id=_COMMUNITY,
        server_id=server.id,
        slug="amber-falcon-42",
        authorize=_authorize_allow,
    )
    assert updated.slug == "amber-falcon-42"


async def test_rename_slug_invalid_raises_422_error() -> None:
    server = _server()
    repo = FakeServerRepository()
    repo.seed(server)
    use_case = _make_update_use_case(repo)

    with pytest.raises(InvalidSlugError):
        await use_case(
            community_id=_COMMUNITY,
            server_id=server.id,
            slug="INVALID-UPPER",
            authorize=_authorize_allow,
        )


async def test_rename_slug_reserved_raises_invalid_error() -> None:
    server = _server()
    repo = FakeServerRepository()
    repo.seed(server)
    use_case = _make_update_use_case(repo)

    with pytest.raises(InvalidSlugError):
        await use_case(
            community_id=_COMMUNITY,
            server_id=server.id,
            slug="api",
            authorize=_authorize_allow,
        )


async def test_rename_slug_taken_raises_conflict() -> None:
    server1 = _server(slug="amber-falcon-42")
    server2 = _server(slug="cedar-wolf-07")
    repo = FakeServerRepository()
    repo.seed(server1)
    repo.seed(server2)
    use_case = _make_update_use_case(repo)

    with pytest.raises(SlugAlreadyTakenError):
        await use_case(
            community_id=_COMMUNITY,
            server_id=server1.id,
            slug="cedar-wolf-07",  # taken by server2
            authorize=_authorize_allow,
        )


async def test_rename_slug_requires_server_update_permission() -> None:
    server = _server()
    repo = FakeServerRepository()
    repo.seed(server)
    use_case = _make_update_use_case(repo)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await use_case(
            community_id=_COMMUNITY,
            server_id=server.id,
            slug="cedar-wolf-07",
            authorize=_authorize_deny,
        )
    assert exc_info.value.permission == "server:update"


async def test_rename_slug_allowed_while_running() -> None:
    """Slug rename does not require the server to be at rest (routing is consulted
    only at join time; renaming while running is safe — RELAY.md Section 3)."""
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    repo = FakeServerRepository()
    repo.seed(server)
    use_case = _make_update_use_case(repo)

    updated = await use_case(
        community_id=_COMMUNITY,
        server_id=server.id,
        slug="cedar-wolf-07",
        authorize=_authorize_allow,
    )
    assert updated.slug == "cedar-wolf-07"


# ---------------------------------------------------------------------------
# Backfill slug format (migration helper)
# ---------------------------------------------------------------------------


def test_backfill_slug_format_is_valid_dns_label() -> None:
    """Every wordlist combination produces a valid slug."""
    import random as rnd

    for _ in range(100):
        candidate = (
            f"{rnd.choice(_WORDS_A)}-{rnd.choice(_WORDS_B)}-{rnd.randint(0, 99):02d}"
        )
        validate_slug(candidate)
