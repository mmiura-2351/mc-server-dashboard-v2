#!/usr/bin/env python3
"""Guard: no control-plane service sits on the MC-server network (#2619).

The Minecraft server containers the worker creates run third-party plugin code,
so ``compose.yaml`` gives them a network of their own, off the one api, db and
seaweedfs share (#2590). That topology exists only in ``compose.yaml`` -- the
worker takes its target network verbatim from its environment -- so this
renders the file with ``docker compose config`` and fails unless:

1. The services on the network the worker attaches MC containers to are
   exactly ``MEMBERS``. Membership is enumerated, so any addition fails, and it
   is resolved by the network's Docker name, so a second key naming the same
   network counts too. No service may set ``network_mode``: it joins a network
   without listing it (``service:worker`` shares the worker's).
2. The network names derive from the project name, ``<project>`` and
   ``<project>-servers``, and the worker's target is the latter (#2609): a
   literal name puts every other stack on the live deployment's network.

The render uses a throwaway project name (``-p``), ``.env.example`` as the env
file (so a checkout's ``.env`` is never read), placeholders for the ``:?``
variables and every profile (``--profile "*"``), so a profile-gated service is
checked too. ``config`` only parses: it needs the docker CLI with the compose
plugin, not a daemon, and starts nothing.

Run ``scripts/check_compose_topology.py --self-test`` to exercise the checks
against fixtures (no docker needed).
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

# The only service allowed on the MC containers' network: it reaches their RCON
# and game ports by container name there.
MEMBERS = {"worker"}
TARGET_ENV = "MCD_WORKER_DRIVER_CONTAINER_NETWORK"


def render(root: Path, project: str) -> dict[str, Any]:
    """``compose.yaml`` as ``docker compose config`` resolves it."""
    text = (root / "compose.yaml").read_text(encoding="utf-8")
    required = {var: "placeholder" for var in re.findall(r"\$\{(\w+):?\?", text)}
    cmd = [
        *("docker", "compose", "-f", "compose.yaml", "-p", project),
        *("--env-file", ".env.example", "--profile", "*"),
        *("config", "--format", "json"),
    ]
    done = subprocess.run(
        cmd, cwd=root, env={**os.environ, **required}, capture_output=True, text=True
    )
    if done.returncode != 0:
        sys.exit(f"check-compose-topology: `{' '.join(cmd)}` failed:\n{done.stderr}")
    return json.loads(done.stdout)


def check(config: dict[str, Any], project: str) -> list[str]:
    """Violation messages (empty when the rendered topology holds)."""
    errors: list[str] = []
    networks = config.get("networks") or {}
    services = config.get("services") or {}
    servers = f"{project}-servers"
    for key, want in (("default", project), ("servers", servers)):
        got = (networks.get(key) or {}).get("name")
        if got != want:
            errors.append(
                f"networks.{key}.name renders {got!r}, expected {want!r} -- derive "
                "it from ${COMPOSE_PROJECT_NAME} (#2609)"
            )
    worker = services.get("worker") or {}
    target = (worker.get("environment") or {}).get(TARGET_ENV)
    if target != servers:
        errors.append(
            f"worker {TARGET_ENV} renders {target!r}, expected {servers!r} -- the "
            "worker must attach MC containers to networks.servers (#2609)"
        )
    # Wherever the worker actually sends the MC containers, by Docker name.
    keys = {key for key, net in networks.items() if (net or {}).get("name") == target}
    on = {
        name for name, svc in services.items() if keys & set(svc.get("networks") or ())
    }
    if on != MEMBERS:
        errors.append(
            f"services on the MC containers' network {target!r} are {sorted(on)}, "
            f"expected exactly {sorted(MEMBERS)} -- MC containers run untrusted "
            "code; keep every other service off their network (#2590)"
        )
    for name, svc in services.items():
        if svc.get("network_mode"):
            errors.append(
                f"services.{name}.network_mode is {svc['network_mode']!r} -- it "
                "joins networks without listing them; attach via `networks:`"
            )
    return errors


def main() -> int:
    project = f"topology-check-{uuid.uuid4().hex[:8]}"
    errors = check(render(Path(__file__).resolve().parent.parent, project), project)
    if errors:
        print(
            "check-compose-topology: compose.yaml breaks the MC-server network "
            f"segmentation (rendered as project {project!r}):",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    print(
        f"check-compose-topology: OK ({project}-servers holds only {sorted(MEMBERS)})"
    )
    return 0


def _self_test() -> int:
    """Exercise ``check`` against rendered-config fixtures (no docker)."""
    clean = {
        "name": "p",
        "networks": {"default": {"name": "p"}, "servers": {"name": "p-servers"}},
        "services": {
            "api": {"networks": {"default": None}},
            "db": {"networks": {"default": None}},
            "worker": {
                "environment": {TARGET_ENV: "p-servers"},
                "networks": {"default": {"gw_priority": 100}, "servers": {}},
            },
        },
    }
    failures: list[str] = []

    def expect(
        name: str, mutate: Callable[[dict[str, Any]], None], *parts: str
    ) -> None:
        """``check`` on ``clean`` after ``mutate`` names every one of ``parts``."""
        config = copy.deepcopy(clean)
        mutate(config)
        got = check(config, "p")
        report = "\n".join(got)
        if bool(got) != bool(parts) or not all(part in report for part in parts):
            failures.append(f"{name}: expected {parts!r}, got {got!r}")

    expect("clean", lambda c: None)
    # The #2590 regression: a control-plane service back on the servers network.
    expect(
        "api on servers",
        lambda c: c["services"]["api"]["networks"].update(servers=None),
        "['api', 'worker']",
    )

    # Resolved by name: a second key naming the same Docker network is the same
    # network.
    def alias(c: dict[str, Any]) -> None:
        c["networks"]["mc"] = {"name": "p-servers", "external": True}
        c["services"]["db"]["networks"] = {"mc": None}

    expect("db on an alias of servers", alias, "['db', 'worker']")
    expect(
        "network_mode shares the worker's namespace",
        lambda c: c["services"]["api"].update(
            networks=None, network_mode="service:worker"
        ),
        "services.api.network_mode",
    )
    expect(
        "worker off servers",
        lambda c: c["services"]["worker"]["networks"].pop("servers"),
        "[]",
    )

    # #2609: literals instead of the project-derived names, even when the worker
    # and the network agree with each other.
    def literal(c: dict[str, Any]) -> None:
        c["networks"]["servers"]["name"] = "mcsd-servers"
        c["services"]["worker"]["environment"][TARGET_ENV] = "mcsd-servers"

    expect("literal servers name", literal, "networks.servers.name", TARGET_ENV)
    expect(
        "literal default name",
        lambda c: c["networks"]["default"].update(name="mcsd"),
        "networks.default.name",
    )

    # The servers network dropped and the worker left on the control plane's:
    # everything there shares a network with the MC containers.
    def fallback(c: dict[str, Any]) -> None:
        del c["networks"]["servers"]
        c["services"]["worker"]["networks"].pop("servers")
        c["services"]["worker"]["environment"][TARGET_ENV] = "p"

    expect(
        "fallback to default",
        fallback,
        "networks.servers.name",
        TARGET_ENV,
        "['api', 'db', 'worker']",
    )
    expect(
        "worker target unset",
        lambda c: c["services"]["worker"]["environment"].pop(TARGET_ENV),
        TARGET_ENV,
    )

    if failures:
        print("check_compose_topology --self-test FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("check_compose_topology --self-test: OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit(main())
