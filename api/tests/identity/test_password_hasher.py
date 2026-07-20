"""Tests for the PasswordHasher adapters (argon2 primary, bcrypt alternative).

The hash must never be the plaintext, must verify against the algorithm's own
verifier, and must be salted (two hashes of the same password differ). The KDF
work also runs off the event loop (issue #938): both methods are async and
offload to a worker thread, so a hash never stalls in-flight requests.
"""

from __future__ import annotations

import asyncio
import threading

import argon2
import bcrypt
import pytest

from mc_server_dashboard_api.identity.adapters.password_hasher import (
    Argon2PasswordHasher,
    BcryptPasswordHasher,
)


async def test_argon2_hash_is_not_plaintext_and_verifies() -> None:
    hasher = Argon2PasswordHasher()
    hashed = await hasher.hash("Wm7!qz#Lp2vT")
    assert hashed != "Wm7!qz#Lp2vT"
    assert argon2.PasswordHasher().verify(hashed, "Wm7!qz#Lp2vT") is True


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
    assert await hasher.verify("Wm7!qz#Lp2vT", hashed) is True

    assert hasher.hash_thread is not None
    assert hasher.verify_thread is not None
    assert hasher.hash_thread is not loop_thread
    assert hasher.verify_thread is not loop_thread


async def test_kdf_runs_on_dedicated_pool() -> None:
    """KDF work runs on the dedicated ``password-kdf`` pool, not the default (#1696)."""
    hasher = _ThreadProbeHasher()
    hashed = await hasher.hash("Wm7!qz#Lp2vT")
    await hasher.verify("Wm7!qz#Lp2vT", hashed)

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
        result = await asyncio.wait_for(
            hasher.verify("Wm7!qz#Lp2vT", hashed), timeout=5
        )
        assert result is True
    finally:
        blocker.set()
        concurrent.futures.wait(futs)
        default_pool.shutdown(wait=True)
