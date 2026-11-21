"""Each context's "imports no other context" contract keeps up with the contexts.

Every bounded context's domain and application carry a contract in
``api/pyproject.toml`` that forbids the other contexts, and each one names
those contexts by hand, so a new context is unguarded until someone remembers
to add it to every list (issue #3954). These tests hold each list to the
packages under ``src/mc_server_dashboard_api/``, less the omissions the
contracts' comments document.

The contracts' ``source_modules`` are listed by hand too: each context's own,
and the root-level-module contract's, which names every context's inward
layers at once. A context's new ``domain`` or ``application`` package is
unguarded until someone adds it to both (issue #3968), so these tests hold
each list to the packages on disk, plus the extra inward packages the
contracts' comments document.

So are each context's layered contract's ``layers`` and its adapters
contract's ``source_modules`` (issue #3991). These tests derive both from the
packages on disk as well: the layers are whichever of ``api``,
``application`` and ``domain`` the context has, and the adapters contract
covers its inward packages. Everywhere here, a package exists when its
directory has an ``__init__.py``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = "mc_server_dashboard_api"
SOURCE = ROOT / "api" / "src" / PACKAGE

ROOT_LEVEL_CONTRACT = "no context's domain or application imports a root-level module"

# The siblings a context's contract leaves out, for the reasons its comment
# gives there.
OMITTED = {"servers": {"audit"}}

# The packages besides `domain` and `application` on a context's inward side,
# for the reasons the contracts' comments give there.
INWARD_EXTRAS = {"storage": {"integrity"}}

# A context's layers, top to bottom, as its layered contract lists them.
LAYERS = ["api", "application", "domain"]


def _contexts() -> set[str]:
    return {path.parent.name for path in SOURCE.glob("*/__init__.py")}


def _is_package(context: str, name: str) -> bool:
    # A package exists when its directory has an `__init__.py`: a directory
    # without one (say, a removed package's leftover `__pycache__`) is none.
    return (SOURCE / context / name / "__init__.py").is_file()


def _inward_contexts() -> set[str]:
    # A context with neither a `domain` nor an `application` package has no
    # inward layer for the contract to cover (the `dataplane` note there).
    return {
        context
        for context in _contexts()
        if _is_package(context, "domain") or _is_package(context, "application")
    }


def _inward_packages(context: str) -> set[str]:
    return {
        f"{PACKAGE}.{context}.{layer}"
        for layer in {"domain", "application"} | INWARD_EXTRAS.get(context, set())
        if _is_package(context, layer)
    }


def _layers(context: str) -> list[str]:
    return [
        f"{PACKAGE}.{context}.{layer}"
        for layer in LAYERS
        if _is_package(context, layer)
    ]


def _contracts() -> list[dict[str, Any]]:
    with (ROOT / "api" / "pyproject.toml").open("rb") as file:
        config = tomllib.load(file)
    contracts: list[dict[str, Any]] = config["tool"]["importlinter"]["contracts"]
    return contracts


def _cross_context_contracts() -> list[tuple[str, set[str], set[str]]]:
    # Each contract's context, read off its name, its source modules, and the
    # contexts it forbids.
    return [
        (
            contract["name"].partition(" ")[0],
            set(contract["source_modules"]),
            {
                module.removeprefix(f"{PACKAGE}.")
                for module in contract["forbidden_modules"]
                if module.startswith(f"{PACKAGE}.")
            },
        )
        for contract in _contracts()
        if "no other context" in contract["name"]
    ]


def test_every_context_with_an_inward_layer_has_one_cross_context_contract() -> None:
    named = sorted(context for context, _, _ in _cross_context_contracts())

    assert named == sorted(_inward_contexts())


def test_every_cross_context_contract_forbids_exactly_the_other_contexts() -> None:
    contexts = _contexts()
    drift: dict[str, dict[str, list[str]]] = {}
    for context, _, forbidden in _cross_context_contracts():
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


def test_every_cross_context_contract_covers_exactly_its_inward_packages() -> None:
    drift: dict[str, dict[str, list[str]]] = {}
    for context, sources, _ in _cross_context_contracts():
        expected = _inward_packages(context)
        if sources != expected:
            drift[context] = {
                "missing": sorted(expected - sources),
                "extra": sorted(sources - expected),
            }

    assert drift == {}


def test_the_root_level_contract_covers_exactly_the_inward_packages() -> None:
    [contract] = [c for c in _contracts() if c["name"] == ROOT_LEVEL_CONTRACT]
    sources = set(contract["source_modules"])
    expected = set().union(*(_inward_packages(context) for context in _contexts()))
    drift = {"missing": sorted(expected - sources), "extra": sorted(sources - expected)}

    assert drift == {"missing": [], "extra": []}


def test_every_inward_extra_is_a_package() -> None:
    missing = [
        f"{context}.{layer}"
        for context, layers in INWARD_EXTRAS.items()
        for layer in sorted(layers)
        if not _is_package(context, layer)
    ]

    assert missing == []


def test_the_layered_contracts_list_exactly_the_layers_on_disk() -> None:
    # A context with fewer than two layers has no layered contract: one layer
    # orders nothing (the `storage` and `dataplane` notes there).
    expected = sorted(
        (context, layers)
        for context in _contexts()
        if len(layers := _layers(context)) >= 2
    )
    listed = sorted(
        (contract["name"].partition(" ")[0], contract["layers"])
        for contract in _contracts()
        if contract["type"] == "layers"
    )

    assert listed == expected


def test_the_adapters_contracts_cover_exactly_the_inward_packages_on_disk() -> None:
    expected = sorted(
        (context, sorted(_inward_packages(context)))
        for context in _contexts()
        if _is_package(context, "adapters") and _inward_packages(context)
    )
    listed = sorted(
        (contract["name"].partition(" ")[0], sorted(contract["source_modules"]))
        for contract in _contracts()
        if "not import adapters" in contract["name"]
    )

    assert listed == expected
