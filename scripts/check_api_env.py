#!/usr/bin/env python3
"""Preflight for the api/ gate: fail loud when the active Python environment
resolves ``mc_server_dashboard_api`` from a *different* checkout than this one.

Agent worktrees under ``.claude/worktrees/`` inherit ``VIRTUAL_ENV`` pointing at
the primary checkout's ``api/.venv``. Depending on the ``uv`` version, ``uv run``
may then type-check / test against the primary checkout's sources (on ``main``)
instead of the worktree's branch, emitting only a benign-looking ``VIRTUAL_ENV
... does not match`` warning that agents ignore. The result is a false-passing
local ``make check`` that fails CI against the real sources (#566).

This check resolves the importable ``mc_server_dashboard_api`` package and
verifies its source lives under *this* checkout's ``api/src``. On mismatch it
exits non-zero with the ``cd api && uv sync`` fix instruction. Unlike the
``hooks-check`` warning, a wrong-source gate invalidates everything after it, so
this FAILS rather than warns.

The expected ``api/src`` is anchored to this script's own location (the checkout
it is checked into), not the working directory, so the comparison stays correct
no matter where ``uv run`` is invoked from.

On a fresh checkout (CI runners, the primary checkout) there is no shadowing and
the package resolves under the local ``api/src``, so this is a silent no-op.

Run ``scripts/check_api_env.py --self-test`` to exercise the comparison logic
against in-memory fixtures (pure standard library, no import of the api package).
"""

from __future__ import annotations

import pathlib
import sys

# Resolved path to this script's checkout's api/src (scripts/ -> <root>/api/src).
_CHECKOUT_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPECTED_API_SRC = (_CHECKOUT_ROOT / "api" / "src").resolve()


def env_violation(
    module_file: pathlib.Path, expected_api_src: pathlib.Path
) -> str | None:
    """Return an actionable error message when ``module_file`` does not live
    under ``expected_api_src``, else ``None``.

    Both paths are resolved (symlinks + ``..`` collapsed) before the prefix
    comparison so an editable install and a worktree path compare correctly.
    """
    module_file = module_file.resolve()
    expected_api_src = expected_api_src.resolve()
    if expected_api_src in module_file.parents:
        return None
    return (
        "api environment shadowing detected (#566): the active Python "
        "environment resolves 'mc_server_dashboard_api' from\n"
        f"  {module_file}\n"
        "but this checkout's sources are under\n"
        f"  {expected_api_src}\n"
        "The api gate would check the wrong sources (a false pass). Sync the "
        "worktree's own environment first:\n"
        "  cd api && uv sync"
    )


def _self_test() -> int:
    api_src = pathlib.Path("/repo/api/src")

    # Module under the expected src -> no violation.
    ok = api_src / "mc_server_dashboard_api" / "__init__.py"
    assert env_violation(ok, api_src) is None, "in-checkout module must pass"

    # Module under a sibling checkout's src -> violation with fix instruction.
    shadowed = pathlib.Path("/primary/api/src/mc_server_dashboard_api/__init__.py")
    msg = env_violation(shadowed, api_src)
    assert msg is not None, "shadowed module must fail"
    assert "uv sync" in msg, "violation must carry the fix instruction"

    # The expected src dir itself is not 'under' itself -> treated as a mismatch.
    assert env_violation(api_src, api_src) is not None, "src dir is not a module"

    print("check_api_env self-test: ok")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return _self_test()

    import mc_server_dashboard_api

    module_file = pathlib.Path(mc_server_dashboard_api.__file__)
    violation = env_violation(module_file, EXPECTED_API_SRC)
    if violation is not None:
        print(violation, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
