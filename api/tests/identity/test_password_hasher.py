"""Tests for the PasswordHasher adapters (argon2 primary, bcrypt alternative).

The hash must never be the plaintext, must verify against the algorithm's own
verifier, and must be salted (two hashes of the same password differ).
"""

from __future__ import annotations

import argon2
import bcrypt

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


def test_bcrypt_handles_password_over_72_bytes() -> None:
    # bcrypt has a hard 72-byte input cap; the adapter truncates so a long but
    # policy-valid password (max_length up to 128 chars) does not raise.
    hasher = BcryptPasswordHasher()
    long_password = "A1!" + "x" * 100
    hashed = hasher.hash(long_password)
    assert bcrypt.checkpw(long_password.encode("utf-8")[:72], hashed.encode("utf-8"))
