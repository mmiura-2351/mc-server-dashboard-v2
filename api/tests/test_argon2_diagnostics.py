"""The argon2 mismatch report a refused correct password fails with (#3748).

A correct password has intermittently failed to verify against a hash the same
process had just produced. The report is what that failure carries, so each of
its sections has to be there the one time it is needed.
"""

from __future__ import annotations

import argon2
import pytest

from tests.argon2_diagnostics import argon2_mismatch_report


def test_a_refused_password_reports_every_section(
    request: pytest.FixtureRequest,
) -> None:
    stored = argon2.PasswordHasher().hash("Wm7!qz#Lp2vT")

    report = argon2_mismatch_report(
        password="Np4@xZ#Lq9wR", stored_hash=stored, produced_hash=stored
    )

    for section in (
        "--- stored hash ---",
        "--- stored hash verified 5x on this thread ---",
        "--- stored hash verified 3x on the KDF executor ---",
        "--- fresh hash+verify round trips 3x on this thread ---",
        "--- fresh hash+verify round trips 3x on the KDF executor ---",
        "--- process ---",
        "--- tests this process started, oldest first ---",
        "--- argon2 build ---",
    ):
        assert section in report
    assert repr(stored) in report
    assert "equals the hash the test produced: True" in report
    # Each of the 8 probes of the stored hash saw the wrong password refused.
    assert report.count("VerifyMismatchError") == 8
    assert "password-kdf" in report
    # The conftest hook recorded this test as the last one this process started.
    assert request.node.nodeid in report
