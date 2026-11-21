"""The root-level-module import contract keeps up with the modules (issue #3945).

The contract in ``api/pyproject.toml`` that bars every context's domain and
application from the root-level modules names those modules by hand, so a new
one is unguarded until someone remembers to add it. These tests hold the list
to ``src/mc_server_dashboard_api/*.py``, less the neutral modules its comment
exempts.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = "mc_server_dashboard_api"
CONTRACT = "no context's domain or application imports a root-level module"

# The modules the contract's comment exempts, for the reasons it gives there.
EXEMPT = {"rfc3339", "archive_member", "gc_pass", "name_characters"}


def _root_modules() -> set[str]:
    package = ROOT / "api" / "src" / PACKAGE
    return {path.stem for path in package.glob("*.py")} - {"__init__"}


def _forbidden_modules() -> set[str]:
    with (ROOT / "api" / "pyproject.toml").open("rb") as file:
        contracts = tomllib.load(file)["tool"]["importlinter"]["contracts"]
    [contract] = [c for c in contracts if c["name"] == CONTRACT]
    return set(contract["forbidden_modules"])


def test_every_root_level_module_is_forbidden_unless_exempt() -> None:
    expected = {f"{PACKAGE}.{name}" for name in _root_modules() - EXEMPT}

    assert sorted(expected - _forbidden_modules()) == []


def test_every_exemption_is_a_root_level_module() -> None:
    assert sorted(EXEMPT - _root_modules()) == []
