#!/usr/bin/env python3
"""Seed the relay protocol-level E2E fixtures (epic #659, issue #962).

Registers the first user (auto-granted platform admin, issue #909), provisions a
community owned by that user, and creates a STOPPED vanilla server. The server's
slug is printed on stdout so the orchestrator (scripts/run_relay_e2e.sh) can hand
it to the Go protocol client as the stopped-server hostname label.

The server is created but never started: a freshly-created server sits at
``observed_state = stopped`` with no assigned worker, which is exactly the
``ResolveJoin`` STOPPED case the relay answers in-protocol (RELAY.md Section 7).

Uses only the Python standard library (urllib) so it needs no extra dependency in
the orchestrator's environment.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_URL = os.environ.get("MCD_RELAY_E2E_API_URL", "http://127.0.0.1:8081")
USERNAME = "relay-e2e-admin"
PASSWORD = "RelayE2eAdmin!234"
EMAIL = "relay-e2e-admin@example.com"


def _request(
    method: str, path: str, *, token: str | None = None, body: dict | None = None
) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"{API_URL}{path}", data=data, method=method)
    req.add_header("content-type", "application/json")
    if token is not None:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 (trusted local URL)
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> None:
    # Register the first user — the API auto-grants platform admin (#909). A 409
    # means a previous run already seeded it; either way we then log in.
    status, _ = _request(
        "POST",
        "/api/users",
        body={"username": USERNAME, "email": EMAIL, "password": PASSWORD},
    )
    if status not in (201, 409):
        sys.exit(f"register failed: {status}")

    status, login = _request(
        "POST", "/api/auth/login", body={"username": USERNAME, "password": PASSWORD}
    )
    if status != 200:
        sys.exit(f"login failed: {status} {login}")
    token = login["access_token"]

    status, me = _request("GET", "/api/users/me", token=token)
    if status != 200 or not me.get("is_platform_admin"):
        sys.exit("first user was not auto-granted platform admin (#909)")
    admin_user_id = me["id"]

    # Provision a community owned by the admin (the platform-admin-only route).
    status, community = _request(
        "POST",
        "/api/communities",
        token=token,
        body={"name": "relay-e2e", "owner_user_id": admin_user_id},
    )
    if status != 201:
        sys.exit(f"community create failed: {status} {community}")
    community_id = community["id"]

    # Create a vanilla server and leave it STOPPED (never start it). The version
    # is catalog-validated at create time against Mojang's live version manifest
    # (https://launchermeta.mojang.com/mc/game/version_manifest_v2.json), so the
    # API container needs outbound HTTPS access to Mojang's version manifest host.
    # On network-isolated runners (no egress) seeding will fail at this call with
    # a catalog fetch error.
    # No JAR download happens here — that is deferred to the first server start.
    status, server = _request(
        "POST",
        f"/api/communities/{community_id}/servers",
        token=token,
        body={
            "name": "relay-e2e-stopped",
            "mc_edition": "java",
            "mc_version": "1.21.1",
            "server_type": "vanilla",
            "execution_backend": "container",
            "accept_eula": True,
        },
    )
    if status != 201:
        sys.exit(f"server create failed: {status} {server}")

    # Only the slug goes to stdout; everything else is progress noise on stderr.
    print(server["slug"])


if __name__ == "__main__":
    main()
