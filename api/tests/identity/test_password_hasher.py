"""Tests for the PasswordHasher adapters (argon2 primary, bcrypt alternative).

The hash must never be the plaintext, must verify against the algorithm's own
verifier, and must be salted (two hashes of the same password differ). The KDF
work also runs off the event loop (issue #938): both methods are async and
offload to a worker thread, so a hash never stalls in-flight requests.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Literal

import argon2
import bcrypt
import pytest

from mc_server_dashboard_api.identity.adapters.password_hasher import (
    _MALFORMED_HASH_ERRORS,
    Argon2PasswordHasher,
    BcryptPasswordHasher,
)
from mc_server_dashboard_api.identity.domain.errors import (
    PasswordVerificationUnavailableError,
)
from tests.argon2_diagnostics import RusageWindow, argon2_mismatch_report


async def test_argon2_hash_is_not_plaintext_and_verifies() -> None:
    hasher = Argon2PasswordHasher()
    hashed = await hasher.hash("Wm7!qz#Lp2vT")
    assert hashed != "Wm7!qz#Lp2vT"
    window = RusageWindow()
    try:
        with window:
            assert argon2.PasswordHasher().verify(hashed, "Wm7!qz#Lp2vT") is True
    except argon2.exceptions.VerifyMismatchError as exc:
        # The intermittent refusal of #3748: fail with its report.
        raise AssertionError(
            argon2_mismatch_report(
                password="Wm7!qz#Lp2vT", stored_hash=hashed, rusage=window
            )
        ) from exc


async def test_argon2_salts_each_hash() -> None:
    hasher = Argon2PasswordHasher()
    assert await hasher.hash("Wm7!qz#Lp2vT") != await hasher.hash("Wm7!qz#Lp2vT")


async def test_argon2_verify_returns_false_on_malformed_stored_hash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A corrupted/garbage stored hash makes argon2 raise InvalidHash (not a
    # VerifyMismatchError); verify() must return False so the auth route keeps a
    # uniform 401 instead of a 500, and the data-corruption signal is logged.
    hasher = Argon2PasswordHasher()
    with caplog.at_level("WARNING"):
        result = await hasher.verify("Wm7!qz#Lp2vT", "not-a-valid-argon2-hash")
    assert result is False
    assert any(record.levelname == "WARNING" for record in caplog.records)


# A well-formed argon2id hash, cut into its fields so a test can corrupt one.
_ARGON2_HEAD = "$argon2id$v=19"
_ARGON2_PARAMS = "m=65536,t=3,p=4"
_ARGON2_SALT = "MpDmDyRnsUyW+VHLyrjk3g"
_ARGON2_TAG = "LtCu+pyMD8sKSqyesD5dMylj5jooa+Fwl1cGFZ8sTAc"


def _argon2_hash(
    *, params: str = _ARGON2_PARAMS, salt: str = _ARGON2_SALT, tag: str = _ARGON2_TAG
) -> str:
    return f"{_ARGON2_HEAD}${params}${salt}${tag}"


async def test_argon2_verify_returns_false_on_a_wrong_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hasher = Argon2PasswordHasher()
    stored = await hasher.hash("Wm7!qz#Lp2vT")
    with caplog.at_level("WARNING"):
        assert await hasher.verify("Np4@xZ#Lq9wR", stored) is False
    assert caplog.records == []


@pytest.mark.parametrize(
    "stored",
    [
        # Each is refused by libargon2 itself, after the hash parsed far enough
        # for argon2.extract_parameters to accept it (issue #3749).
        pytest.param(_argon2_hash(tag=_ARGON2_TAG[:-10]), id="truncated-tag"),
        pytest.param(_argon2_hash(tag=_ARGON2_TAG[:-3] + "!!!"), id="bad-base64"),
        pytest.param(_argon2_hash(tag="AAA"), id="tag-too-short"),
        pytest.param(_argon2_hash(salt="AAAA"), id="salt-too-short"),
        pytest.param(_argon2_hash(params="m=1,t=3,p=4"), id="memory-too-small"),
        pytest.param(_argon2_hash(params="m=65536,t=0,p=4"), id="time-too-small"),
        pytest.param(_argon2_hash(params="m=65536,t=3,p=0"), id="too-few-lanes"),
        # One lane past libargon2's maximum, with the 8 KiB a lane needs, so the
        # lane check is the one that fails, before any allocation (#3762).
        pytest.param(
            _argon2_hash(params="m=134217728,t=3,p=16777216"), id="too-many-lanes"
        ),
        pytest.param(_argon2_hash(params="m=8589934592,t=3,p=4"), id="memory-overflow"),
        # argon2-cffi refuses a non-ASCII hash before libargon2 sees it.
        pytest.param(_argon2_hash(salt="MpDmDyRnsUyW+VHLyrjk3é"), id="non-ascii"),
    ],
)
async def test_argon2_verify_refuses_a_corrupt_stored_hash(
    stored: str, caplog: pytest.LogCaptureFixture
) -> None:
    # A stored hash libargon2 cannot decode or whose parameters it rejects is
    # corrupt data, not a server failure: the login is refused with the uniform
    # 401 and the corruption is surfaced as a WARNING.
    hasher = Argon2PasswordHasher()
    with caplog.at_level("WARNING"):
        result = await hasher.verify("Wm7!qz#Lp2vT", stored)
    assert result is False
    assert [record.levelname for record in caplog.records] == ["WARNING"]


def test_malformed_hash_messages_are_libargon2_messages() -> None:
    # The corrupt-hash refusal matches libargon2's messages by text; a typo would
    # silently turn that corruption into a 503 instead of a WARNING and a 401.
    libargon2_messages = {argon2.low_level.error_to_str(code) for code in range(-40, 0)}
    assert _MALFORMED_HASH_ERRORS <= libargon2_messages


class _FailingArgon2(argon2.PasswordHasher):
    """argon2-cffi whose libargon2 call fails with ``message``, as out of memory."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str:
        raise argon2.exceptions.HashingError(self._message)

    def verify(self, hash: str | bytes, password: str | bytes) -> Literal[True]:
        raise argon2.exceptions.VerificationError(self._message)


@pytest.mark.parametrize("message", ["Memory allocation error", "Threading failure"])
async def test_argon2_verify_raises_when_the_verifier_itself_fails(
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # libargon2 reports running out of memory or threads as a VerificationError,
    # the same class as a corrupt hash. On a well-formed hash it is a server
    # failure, not a wrong password: verify() must raise rather than return False,
    # or a correct password is refused and counted towards a lockout (#3749).
    hasher = Argon2PasswordHasher()
    stored = await hasher.hash("Wm7!qz#Lp2vT")
    monkeypatch.setattr(hasher, "_hasher", _FailingArgon2(message))

    with (
        caplog.at_level("WARNING"),
        pytest.raises(PasswordVerificationUnavailableError),
    ):
        await hasher.verify("Wm7!qz#Lp2vT", stored)
    assert [record.levelname for record in caplog.records] == ["ERROR"]
    # The log carries libargon2's message, never the hash or the password.
    assert message in caplog.text
    assert stored not in caplog.text
    assert "Wm7!qz#Lp2vT" not in caplog.text


@pytest.mark.parametrize("message", ["Memory allocation error", "Threading failure"])
async def test_argon2_hash_raises_when_the_hasher_itself_fails(
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Hashing a new password can run out of memory or threads too. That is the
    # same server failure as on verify, so hash() raises the same error, which
    # every route that hashes answers with a 503 instead of a 500 (#3762).
    hasher = Argon2PasswordHasher()
    monkeypatch.setattr(hasher, "_hasher", _FailingArgon2(message))

    with (
        caplog.at_level("WARNING"),
        pytest.raises(PasswordVerificationUnavailableError),
    ):
        await hasher.hash("Wm7!qz#Lp2vT")
    assert [record.levelname for record in caplog.records] == ["ERROR"]
    # The log carries libargon2's message, never the password.
    assert message in caplog.text
    assert "Wm7!qz#Lp2vT" not in caplog.text


async def test_bcrypt_hash_is_not_plaintext_and_verifies() -> None:
    hasher = BcryptPasswordHasher()
    hashed = await hasher.hash("Wm7!qz#Lp2vT")
    assert hashed != "Wm7!qz#Lp2vT"
    assert bcrypt.checkpw(b"Wm7!qz#Lp2vT", hashed.encode("utf-8")) is True


async def test_bcrypt_salts_each_hash() -> None:
    hasher = BcryptPasswordHasher()
    assert await hasher.hash("Wm7!qz#Lp2vT") != await hasher.hash("Wm7!qz#Lp2vT")


async def test_bcrypt_raises_on_password_over_72_bytes() -> None:
    # The policy rejects >72-byte passwords under bcrypt before they reach the
    # adapter; this defensive guard raises rather than silently truncating, so
    # two distinct passwords sharing a 72-byte prefix can never collide. The
    # ValueError must still propagate out of the worker-thread offload unchanged.
    hasher = BcryptPasswordHasher()
    long_password = "A1!" + "x" * 100
    with pytest.raises(ValueError):
        await hasher.hash(long_password)


async def test_bcrypt_hashes_at_72_byte_boundary() -> None:
    hasher = BcryptPasswordHasher()
    boundary_password = "A1!" + "x" * 69
    assert len(boundary_password.encode("utf-8")) == 72
    hashed = await hasher.hash(boundary_password)
    assert bcrypt.checkpw(boundary_password.encode("utf-8"), hashed.encode("utf-8"))


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("not-a-valid-bcrypt-hash", id="garbage"),
        pytest.param("", id="empty"),
        pytest.param(
            "$9z$12$/ojJOhrhlOxTPk0wZGly0uPr1PwRSHD9pyBm75jkxgIW2hSVoriFy",
            id="unknown-prefix",
        ),
        pytest.param(
            "$2b$99$/ojJOhrhlOxTPk0wZGly0uPr1PwRSHD9pyBm75jkxgIW2hSVoriFy",
            id="cost-out-of-range",
        ),
    ],
)
async def test_bcrypt_verify_refuses_a_corrupt_stored_hash(
    stored: str, caplog: pytest.LogCaptureFixture
) -> None:
    # bcrypt raises ValueError("Invalid salt") on a stored hash it cannot parse.
    # That is corrupt data, refused like a wrong password (the uniform 401)
    # with a WARNING, as under argon2, not a 500 (#3749).
    hasher = BcryptPasswordHasher()
    with caplog.at_level("WARNING"):
        result = await hasher.verify("Wm7!qz#Lp2vT", stored)
    assert result is False
    assert [record.levelname for record in caplog.records] == ["WARNING"]


async def test_bcrypt_verify_returns_false_on_password_over_72_bytes() -> None:
    # Login input is attacker-controlled, so verify() must not raise on a
    # >72-byte password (that would 500 the auth route); it returns False so the
    # uniform-401 posture holds and the length is no oracle.
    hasher = BcryptPasswordHasher()
    stored = await hasher.hash("Wm7!qz#Lp2vT")
    long_password = "A1!" + "x" * 100
    assert await hasher.verify(long_password, stored) is False


class _ThreadProbeHasher(Argon2PasswordHasher):
    """Argon2 adapter that records which thread its KDF body runs on.

    The blocking work lives in the inherited (sync) helpers that the async Port
    methods hand to the dedicated KDF executor; capturing the thread there proves
    the offload actually moved the work off the event loop's thread.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hash_thread: threading.Thread | None = None
        self.verify_thread: threading.Thread | None = None

    def _hash_capture(self, plaintext: str) -> str:
        self.hash_thread = threading.current_thread()
        return self._hasher.hash(plaintext)

    async def hash(self, plaintext: str) -> str:
        from mc_server_dashboard_api.identity.adapters.password_hasher import _run_kdf

        return await _run_kdf(self._hash_capture, plaintext)

    def _verify(self, plaintext: str, password_hash: str) -> bool:
        self.verify_thread = threading.current_thread()
        return super()._verify(plaintext, password_hash)


async def test_hash_and_verify_run_off_the_event_loop_thread() -> None:
    # Regression for issue #938: argon2 is CPU-bound for tens of milliseconds, so
    # it must not run on the event loop's thread (where it would stall every
    # in-flight request). _run_kdf dispatches to a *non-main* worker thread;
    # assert the KDF body observed a different thread than the loop's.
    loop_thread = threading.current_thread()
    hasher = _ThreadProbeHasher()

    hashed = await hasher.hash("Wm7!qz#Lp2vT")
    with RusageWindow() as window:
        verified = await hasher.verify("Wm7!qz#Lp2vT", hashed)
    # The report runs only on a failure, the intermittent refusal of #3748.
    assert verified is True, argon2_mismatch_report(
        password="Wm7!qz#Lp2vT", stored_hash=hashed, rusage=window
    )

    assert hasher.hash_thread is not None
    assert hasher.verify_thread is not None
    assert hasher.hash_thread is not loop_thread
    assert hasher.verify_thread is not loop_thread


async def test_kdf_runs_on_dedicated_pool() -> None:
    """KDF work runs on the dedicated ``password-kdf`` pool, not the default (#1696)."""
    hasher = _ThreadProbeHasher()
    hashed = await hasher.hash("Wm7!qz#Lp2vT")
    with RusageWindow() as window:
        verified = await hasher.verify("Wm7!qz#Lp2vT", hashed)
    # The report runs only on a failure, the intermittent refusal of #3748.
    assert verified is True, argon2_mismatch_report(
        password="Wm7!qz#Lp2vT", stored_hash=hashed, rusage=window
    )

    assert hasher.hash_thread is not None
    assert hasher.verify_thread is not None
    assert hasher.hash_thread.name.startswith("password-kdf")
    assert hasher.verify_thread.name.startswith("password-kdf")


async def test_hasher_not_starved_by_default_executor_saturation() -> None:
    """Starvation regression (#1696): a saturated default executor must not block KDF.

    Parks every slot in asyncio's default ``ThreadPoolExecutor`` with blocking work,
    then verifies that the password hasher still completes within 5 seconds — proving
    it uses a separate pool.
    """
    import concurrent.futures
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    # Saturate the default executor. asyncio uses a ThreadPoolExecutor(max_workers=…)
    # as its default; we replace it with a small, fully-parked one.
    blocker = threading.Event()
    default_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="parked")
    loop.set_default_executor(default_pool)
    # Submit blocking tasks that hold every slot until we set the event.
    futs = [default_pool.submit(blocker.wait) for _ in range(4)]
    try:
        hasher = Argon2PasswordHasher()
        hashed = await hasher.hash("Wm7!qz#Lp2vT")
        with RusageWindow() as window:
            result = await asyncio.wait_for(
                hasher.verify("Wm7!qz#Lp2vT", hashed), timeout=5
            )
        # The report runs only on a failure, the intermittent refusal of #3748.
        assert result is True, argon2_mismatch_report(
            password="Wm7!qz#Lp2vT", stored_hash=hashed, rusage=window
        )
    finally:
        blocker.set()
        concurrent.futures.wait(futs)
        default_pool.shutdown(wait=True)
