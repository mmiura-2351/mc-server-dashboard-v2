"""Each context's "imports no other context" contract keeps up with the contexts.

Every bounded context's domain and application carry a contract in
``api/pyproject.toml`` that forbids the other contexts, and each one names
those contexts by hand, so a new context is unguarded until someone remembers
to add it to every list (issue #3954). These tests hold each list to the
packages under ``src/mc_server_dashboard_api/``, less the omissions the
contracts' comments document.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = "mc_server_dashboard_api"
SOURCE = ROOT / "api" / "src" / PACKAGE

# The siblings a context's contract leaves out, for the reasons its comment
# gives there.
OMITTED = {"servers": {"audit"}}


def _contexts() -> set[str]:
    return {path.parent.name for path in SOURCE.glob("*/__init__.py")}


def _inward_contexts() -> set[str]:
    # A context with neither a `domain` nor an `application` package has no
    # inward layer for the contract to cover (the `dataplane` note there).
    return {
        context
        for context in _contexts()
        if (SOURCE / context / "domain").is_dir()
        or (SOURCE / context / "application").is_dir()
    }


def _cross_context_contracts() -> list[tuple[str, set[str]]]:
    # Each contract's context, read off its name, and the contexts it forbids.
    with (ROOT / "api" / "pyproject.toml").open("rb") as file:
        contracts = tomllib.load(file)["tool"]["importlinter"]["contracts"]
    return [
        (
            contract["name"].partition(" ")[0],
            {
                module.removeprefix(f"{PACKAGE}.")
                for module in contract["forbidden_modules"]
                if module.startswith(f"{PACKAGE}.")
            },
        )
        for contract in contracts
        if "no other context" in contract["name"]
    ]


def test_every_context_with_an_inward_layer_has_one_cross_context_contract() -> None:
    named = sorted(context for context, _ in _cross_context_contracts())

    assert named == sorted(_inward_contexts())


def test_every_cross_context_contract_forbids_exactly_the_other_contexts() -> None:
    contexts = _contexts()
    drift: dict[str, dict[str, list[str]]] = {}
    for context, forbidden in _cross_context_contracts():
        expected = contexts - {context} - OMITTED.get(context, set())
        if forbidden != expected:
            drift[context] = {
                "missing": sorted(expected - forbidden),
                "extra": sorted(forbidden - expected),
            }

    assert drift == {}


def test_every_omission_names_contexts() -> None:
    named = set(OMITTED).union(*OMITTED.values())

    assert sorted(named - _contexts()) == []
