"""Tests for the PasswordHasher adapters (argon2 primary, bcrypt alternative).

The hash must never be the plaintext, must verify against the algorithm's own
verifier, and must be salted (two hashes of the same password differ).
"""

from __future__ import annotations

import argon2
import bcrypt
import pytest

from mc_server_dashboard_api.identity.adapters.password_hasher import (
    Argon2PasswordHasher,
    BcryptPasswordHasher,
)


def test_argon2_hash_is_not_plaintext_and_verifies() -> None:
    hasher = Argon2PasswordHasher()
    hashed = hasher.hash("Wm7!qz#Lp2vT")
    assert hashed != "Wm7!qz#Lp2vT"
    assert argon2.PasswordHasher().verify(hashed, "Wm7!qz#Lp2vT") is True


def test_argon2_salts_each_hash() -> None:
    hasher = Argon2PasswordHasher()
    assert hasher.hash("Wm7!qz#Lp2vT") != hasher.hash("Wm7!qz#Lp2vT")


def test_bcrypt_hash_is_not_plaintext_and_verifies() -> None:
    hasher = BcryptPasswordHasher()
    hashed = hasher.hash("Wm7!qz#Lp2vT")
    assert hashed != "Wm7!qz#Lp2vT"
    assert bcrypt.checkpw(b"Wm7!qz#Lp2vT", hashed.encode("utf-8")) is True


def test_bcrypt_salts_each_hash() -> None:
    hasher = BcryptPasswordHasher()
    assert hasher.hash("Wm7!qz#Lp2vT") != hasher.hash("Wm7!qz#Lp2vT")


def test_bcrypt_raises_on_password_over_72_bytes() -> None:
    # The policy rejects >72-byte passwords under bcrypt before they reach the
    # adapter; this defensive guard raises rather than silently truncating, so
    # two distinct passwords sharing a 72-byte prefix can never collide.
    hasher = BcryptPasswordHasher()
    long_password = "A1!" + "x" * 100
    with pytest.raises(ValueError):
        hasher.hash(long_password)


def test_bcrypt_hashes_at_72_byte_boundary() -> None:
    hasher = BcryptPasswordHasher()
    boundary_password = "A1!" + "x" * 69
    assert len(boundary_password.encode("utf-8")) == 72
    hashed = hasher.hash(boundary_password)
    assert bcrypt.checkpw(boundary_password.encode("utf-8"), hashed.encode("utf-8"))


def test_bcrypt_verify_returns_false_on_password_over_72_bytes() -> None:
    # Login input is attacker-controlled, so verify() must not raise on a
    # >72-byte password (that would 500 the auth route); it returns False so the
    # uniform-401 posture holds and the length is no oracle.
    hasher = BcryptPasswordHasher()
    stored = hasher.hash("Wm7!qz#Lp2vT")
    long_password = "A1!" + "x" * 100
    assert hasher.verify(long_password, stored) is False
