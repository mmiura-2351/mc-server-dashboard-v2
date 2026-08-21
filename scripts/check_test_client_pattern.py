#!/usr/bin/env python3
"""Guard against ``next(_client(...))`` reappearing in the api test suite (#1980).

``next(_client(...))`` over a ``def _client(...): with TestClient(app): yield``
helper finalizes the generator the instant ``next()`` returns, so the app's
lifespan *shutdown* runs before the test body issues its first request -- every
request is served against a torn-down app. The fix replaces the pattern with the
``enter_client`` helper (``api/tests/client_utils.py``), which keeps the lifespan
open until per-test teardown.

The pattern was actively re-proliferating (598 -> 758 call sites between the
issue's filing and its investigation), so this fast grep fails the gate if it
reappears, keeping the fix from eroding.

Scope: all of ``api/tests``. PR 1 converted identity/audit/fleet/core and scanned
only those; PR 2 converted servers/, community/ and integration/ and dropped the
allow-list, so no directory is exempt any more.

Pure standard library; runs under any Python 3.8+ (the api/ venv or a system
python). Exit status is non-zero when the pattern is found.

Run ``scripts/check_test_client_pattern.py --self-test`` to exercise the
detector against fixtures (not the real tree).
"""

from __future__ import annotations

import sys
from pathlib import Path

# The banned call shape. ``_client`` is the endpoint-test client helper; wrapping
# it in ``next(...)`` is the finalize-immediately bug this guards against.
PATTERN = "next(_client("


def find_violations(tests_root: Path) -> list[str]:
    """Return ``path:line`` messages for every ``PATTERN`` occurrence (sorted)."""
    violations: list[str] = []
    for path in sorted(tests_root.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if PATTERN in line:
                rel = path.relative_to(tests_root.parent.parent)
                violations.append(f"{rel}:{lineno}")
    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    tests_root = repo_root / "api" / "tests"
    if not tests_root.is_dir():
        print(f"api tests dir not found at {tests_root}", file=sys.stderr)
        return 2

    violations = find_violations(tests_root)
    if violations:
        print(
            "check-test-client-pattern found the banned `next(_client(...))` "
            "pattern (issue #1980):",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "  Acquire the client via `enter_client(TestClient(app))` "
            "(api/tests/client_utils.py) instead.",
            file=sys.stderr,
        )
        return 1

    print("check-test-client-pattern: OK (scanned api/tests)")
    return 0


def _self_test() -> int:
    """Exercise the detector against fixtures (no real tree dependency)."""
    import tempfile

    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tests_root = Path(tmp) / "api" / "tests"
        (tests_root / "identity").mkdir(parents=True)
        (tests_root / "servers").mkdir(parents=True)

        # A converted file: uses the helper, no violation.
        (tests_root / "identity" / "test_clean.py").write_text(
            "client = _client(login=fake)\n", encoding="utf-8"
        )
        # Offending files anywhere under api/tests are flagged.
        (tests_root / "identity" / "test_bad.py").write_text(
            "x = 1\nclient = next(_client(login=fake))\n", encoding="utf-8"
        )
        (tests_root / "servers" / "test_bad.py").write_text(
            "client = next(_client(app))\n", encoding="utf-8"
        )

        got = find_violations(tests_root)
        want = ["api/tests/identity/test_bad.py:2", "api/tests/servers/test_bad.py:1"]
        if got != want:
            failures.append(f"detection: expected {want!r}, got {got!r}")

        # A clean tree reports nothing.
        (tests_root / "identity" / "test_bad.py").unlink()
        (tests_root / "servers" / "test_bad.py").unlink()
        if find_violations(tests_root):
            failures.append("clean tree: expected no violations")

    if failures:
        print("check_test_client_pattern --self-test FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("check_test_client_pattern --self-test: OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit(main())
