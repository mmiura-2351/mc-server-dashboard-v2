#!/usr/bin/env python3
"""Enforce the 7-day supply-chain cooldown (docs/dev/DEPENDENCIES.md Section 3).

A maintainer-account takeover, typosquat, or compromised release can take
several days to detect and retract, so we do not adopt any upstream release
younger than 7 days. Dependabot already sets ``cooldown: {default-days: 7}`` in
``.github/dependabot.yml``, but that is a single client-side setting on the
PR-opening side; this check is the enforcing gate on the merge side -- it turns
the policy into a required status context that blocks the merge until the
release ages out, and re-evaluates daily so PRs unblock themselves.

For a Dependabot PR it:

1. Reads the bumped packages + versions from the Dependabot commit trailer
   (``updated-dependencies:``) -- one entry per package, so grouped PRs are
   handled -- and the ecosystem from the branch name (``dependabot/<eco>/...``).
2. Looks up each release's publish date in the upstream registry.
3. Posts a ``supply-chain-cooldown`` commit status on the PR head SHA:
   ``failure`` (+ the ``supply-chain-cooldown`` label) while any release is
   younger than 7 days, ``success`` (label removed) once all have aged out.
4. Bypasses the cooldown for Dependabot security updates (a known-exploited
   vulnerability outweighs the supply-chain window -- DEPENDENCIES.md Section 4).

A commit status (not a workflow-job check) is used deliberately: it can be
re-posted on an existing PR head SHA by the daily scheduled run with no new
commit, which is what lets a blocked PR unblock itself after 7 days.

Modes:
  --pr N        Evaluate one PR (the ``pull_request_target`` path).
  --all-open    Evaluate every open Dependabot PR (the schedule / dispatch path).
  --self-test   Exercise the pure logic against in-memory fixtures (no network).

Pure standard library; ``gh`` provides GitHub auth. Registry queries go over
plain HTTPS. Exit status is non-zero only on an unexpected error, never merely
because a PR is blocked -- a blocked PR is a posted status, not a script failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

COOLDOWN_DAYS = 7
STATUS_CONTEXT = "supply-chain-cooldown"
COOLDOWN_LABEL = "supply-chain-cooldown"
BOT = "dependabot[bot]"
_HTTP_TIMEOUT = 30
_USER_AGENT = "mcsd-supply-chain-cooldown"

# Branch ecosystem token (``dependabot/<token>/...``) -> the canonical ecosystem
# used to pick a registry. docker-compose shares docker's registry lookup.
_ECOSYSTEM_BY_TOKEN = {
    "pip": "pip",
    "npm_and_yarn": "npm",
    "github_actions": "github-actions",
    "go_modules": "gomod",
    "docker": "docker",
    "docker-compose": "docker",
    "docker_compose": "docker",
}

# A GHSA identifier -- ``GHSA-xxxx-xxxx-xxxx``. Dependabot embeds the advisory
# reference in a security-update's commit message; a version update's commit
# message does not carry one (the changelog that might mention a CVE lives in the
# PR body's ``<details>`` block, not the commit).
_GHSA = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}", re.IGNORECASE)


class CooldownError(Exception):
    """A release date could not be determined -- treated as blocking."""


# A transport takes a URL / gh-api path and returns parsed JSON, raising on
# failure. Injected so release_date's per-ecosystem field extraction is testable.
Transport = Callable[[str], dict]


# --------------------------------------------------------------------------- #
# Pure parsing / formatting (covered by --self-test)                          #
# --------------------------------------------------------------------------- #


def ecosystem_from_branch(branch: str) -> str | None:
    """Map a Dependabot branch name to its canonical ecosystem, or None."""
    parts = branch.split("/")
    if len(parts) < 2 or parts[0] != "dependabot":
        return None
    return _ECOSYSTEM_BY_TOKEN.get(parts[1])


def parse_updated_dependencies(messages: list[str]) -> list[tuple[str, str]]:
    """Extract (name, version) pairs from Dependabot ``updated-dependencies``.

    Parses the commit trailer Dependabot appends to its commits::

        updated-dependencies:
        - dependency-name: "@scope/pkg"
          dependency-version: 1.2.3
        ...

    Names may be quoted (scoped npm packages). Aggregated across every commit
    message so grouped PRs (multiple entries, one commit) and multi-commit PRs
    both resolve. Order-preserving and de-duplicated.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for message in messages:
        name: str | None = None
        in_block = False
        for raw in message.splitlines():
            if raw.strip() == "updated-dependencies:":
                in_block = True
                continue
            if not in_block:
                continue
            # The block ends at the ``...`` sentinel or any non-indented,
            # non-list line (e.g. the next trailer or the signature).
            if raw.strip() == "..." or (
                raw and not raw[0].isspace() and not raw.startswith("-")
            ):
                in_block = False
                name = None
                continue
            m = re.match(r"\s*-\s*dependency-name:\s*(.+?)\s*$", raw)
            if m:
                name = m.group(1).strip().strip('"').strip("'")
                continue
            m = re.match(r"\s*dependency-version:\s*(.+?)\s*$", raw)
            if m and name is not None:
                version = m.group(1).strip().strip('"').strip("'")
                pair = (name, version)
                if pair not in seen:
                    seen.add(pair)
                    pairs.append(pair)
                name = None
    return pairs


def is_security_update(messages: list[str], body: str) -> bool:
    """Detect a Dependabot security update (bypasses the cooldown).

    A Dependabot *security* update only exists when Dependabot alerts are
    enabled for the repository; it references the advisory it fixes. We look, in
    priority order, at signals Dependabot itself emits:

    1. **Commit message** (immutable, cryptographically attributed to
       ``dependabot[bot]``): a GHSA identifier, or the legacy
       ``dependabot_security_updates`` metadata token. This is the primary
       signal -- the commit is not re-editable the way a PR body is.
    2. **PR body summary** (fallback only): a GHSA identifier or a ``Security``
       heading in the Dependabot-authored summary. The body is truncated at the
       first ``<details>`` block so verbatim *upstream* release notes -- whose
       own "Security" headings or CVE mentions are not evidence of a Dependabot
       security update -- cannot spoof a bypass.
    """
    for message in messages:
        if _GHSA.search(message) or "dependabot_security_updates" in message:
            return True
    summary = body.split("<details>", 1)[0]
    if _GHSA.search(summary):
        return True
    return bool(
        re.search(r"(?im)^\s*(?:#{1,6}\s+|<h[1-6][^>]*>\s*)security\b", summary)
    )


def cooldown_message(
    name: str, version: str, released: datetime, now: datetime
) -> str:
    """The blocking message: '<pkg>@<ver> was released N days ago; ...until D.'."""
    days = (now - released).days
    until = (released + timedelta(days=COOLDOWN_DAYS)).date().isoformat()
    return (
        f"{name}@{version} was released {days} days ago; "
        f"{COOLDOWN_DAYS}-day cooldown requires waiting until {until}."
    )


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (trailing ``Z`` accepted) as tz-aware UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Registry-address construction (pure; covered by --self-test)                #
# --------------------------------------------------------------------------- #


def _gomod_escape(module: str) -> str:
    """Case-encode a Go module path for proxy.golang.org (Foo -> !foo)."""
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), module)


def _go_version(version: str) -> str:
    return version if version.startswith("v") else "v" + version


def _npm_url(name: str) -> str:
    # Encode the scope slash (@scope/name -> @scope%2Fname); the plain packument
    # carries the ``.time`` map that the per-version manifest lacks.
    return "https://registry.npmjs.org/" + name.replace("/", "%2F")


def _github_tag_candidates(version: str) -> list[str]:
    return [f"v{version}", version] if not version.startswith("v") else [version]


def parse_docker_ref(image: str) -> tuple[str, str, str]:
    """Resolve a Docker image reference to (registry, owner_or_ns, repo).

    Docker images are not all on Docker Hub, and the registry host determines the
    lookup:

    * ``ghcr.io/<owner>/<image>`` -> ``("ghcr", owner, image)`` -- resolved via
      the GitHub Releases API on ``<owner>/<image>`` (e.g. the repo's own
      ``ghcr.io/astral-sh/uv`` base image).
    * ``docker.io/<ns>/<repo>``, bare ``<ns>/<repo>``, or bare ``<image>``
      (official -> ``library``) -> ``("dockerhub", ns, repo)``.

    A host token is the first path segment when it contains a ``.`` or ``:``
    (e.g. ``ghcr.io``, ``registry:5000``). An unsupported registry raises
    ``CooldownError`` (fail closed) rather than fabricating a Docker Hub URL.
    """
    first = image.split("/", 1)[0]
    if "." in first or ":" in first:
        host, rest = first, image.split("/", 1)[1]
        if host == "ghcr.io":
            owner, repo = rest.split("/", 1)
            return ("ghcr", owner, repo)
        if host in ("docker.io", "index.docker.io", "registry-1.docker.io"):
            return _dockerhub_ref(rest)
        raise CooldownError(f"unsupported container registry: {host}")
    return _dockerhub_ref(image)


def _dockerhub_ref(path: str) -> tuple[str, str, str]:
    namespace, repo = path.split("/", 1) if "/" in path else ("library", path)
    return ("dockerhub", namespace, repo)


# --------------------------------------------------------------------------- #
# Live transports (network / gh -- swapped for stubs in --self-test)          #
# --------------------------------------------------------------------------- #


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.load(resp)


def _gh_json(path: str) -> dict:
    out = subprocess.run(
        ["gh", "api", path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def _github_release_date(gh_get: Transport, repo: str, version: str) -> datetime:
    """``published_at`` of ``repo``'s release for ``version`` (tries v-prefix)."""
    for tag in _github_tag_candidates(version):
        try:
            data = gh_get(f"repos/{repo}/releases/tags/{tag}")
        except subprocess.CalledProcessError:
            continue  # No such tag -- try the next candidate.
        if data.get("published_at"):
            return parse_iso(data["published_at"])
    raise CooldownError(f"no GitHub release for {repo}@{version}")


def release_date(
    ecosystem: str,
    name: str,
    version: str,
    *,
    http_get: Transport = _http_json,
    gh_get: Transport = _gh_json,
) -> datetime:
    """Upstream publish date for name@version, per docs/dev/DEPENDENCIES.md.

    ``http_get`` / ``gh_get`` are injected so the per-ecosystem field extraction
    (the part that historically hid a bug) is exercised offline in --self-test.
    """
    try:
        if ecosystem == "pip":
            data = http_get(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
            files = data.get("releases", {}).get(version)
            if not files:
                raise CooldownError(f"no PyPI files for {name}@{version}")
            return parse_iso(files[0]["upload_time_iso_8601"])
        if ecosystem == "npm":
            data = http_get(_npm_url(name))
            stamp = data.get("time", {}).get(version)
            if not stamp:
                raise CooldownError(f"no npm publish time for {name}@{version}")
            return parse_iso(stamp)
        if ecosystem == "gomod":
            data = http_get(
                f"https://proxy.golang.org/{_gomod_escape(name)}"
                f"/@v/{_go_version(version)}.info"
            )
            return parse_iso(data["Time"])
        if ecosystem == "github-actions":
            return _github_release_date(gh_get, name, version)
        if ecosystem == "docker":
            registry, owner, repo = parse_docker_ref(name)
            if registry == "ghcr":
                return _github_release_date(gh_get, f"{owner}/{repo}", version)
            data = http_get(
                f"https://hub.docker.com/v2/repositories/{owner}/{repo}"
                f"/tags/{urllib.parse.quote(version)}"
            )
            stamp = data.get("tag_last_pushed") or data.get("last_updated")
            if not stamp:
                raise CooldownError(f"no Docker Hub push date for {name}:{version}")
            return parse_iso(stamp)
        raise CooldownError(f"unsupported ecosystem: {ecosystem}")
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        KeyError,
        ValueError,
    ) as exc:
        raise CooldownError(
            f"could not read release date for {name}@{version}: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# GitHub mutations                                                            #
# --------------------------------------------------------------------------- #


def _gh(args: list[str]) -> None:
    subprocess.run(["gh", *args], check=True, capture_output=True, text=True)


def ensure_label(repo: str) -> None:
    """Create the cooldown label if absent (idempotent; ignores 'exists')."""
    try:
        _gh(
            [
                "api", "-X", "POST", f"repos/{repo}/labels",
                "-f", f"name={COOLDOWN_LABEL}",
                "-f", "color=b60205",
                "-f", "description=Blocked by the 7-day supply-chain cooldown",
            ]
        )
    except subprocess.CalledProcessError:
        pass  # Already exists (422) -- the only expected failure here.


def set_status(repo: str, sha: str, state: str, description: str) -> None:
    _gh(
        [
            "api", "-X", "POST", f"repos/{repo}/statuses/{sha}",
            "-f", f"state={state}",
            "-f", f"context={STATUS_CONTEXT}",
            "-f", f"description={description[:140]}",
        ]
    )


def add_label(repo: str, pr: int) -> None:
    ensure_label(repo)
    _gh(
        [
            "api", "-X", "POST", f"repos/{repo}/issues/{pr}/labels",
            "-f", f"labels[]={COOLDOWN_LABEL}",
        ]
    )


def remove_label(repo: str, pr: int) -> None:
    try:
        _gh(
            [
                "api", "-X", "DELETE",
                f"repos/{repo}/issues/{pr}/labels/{COOLDOWN_LABEL}",
            ]
        )
    except subprocess.CalledProcessError:
        pass  # Not present (404) -- nothing to remove.


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #


def _repo() -> str:
    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    return _gh_json("repos/{owner}/{repo}")["full_name"]


def evaluate_pr(repo: str, pr: int, now: datetime | None = None) -> str:
    """Evaluate one PR, post its status/label, and return the outcome string.

    Fail closed: once the head SHA is known, any unexpected error still posts a
    blocking status (and re-raises so the run surfaces it) -- a check that dies
    silently would leave the required context unset and let the PR merge.
    """
    now = now or datetime.now(timezone.utc)
    meta = _gh_json(f"repos/{repo}/pulls/{pr}")
    if meta["user"]["login"] != BOT:
        return "skipped: not a Dependabot PR"
    sha = meta["head"]["sha"]
    try:
        return _evaluate_head(repo, pr, sha, meta, now)
    except Exception as exc:  # noqa: BLE001 -- fail closed with a posted status
        set_status(repo, sha, "failure", f"cooldown check error: {exc}")
        add_label(repo, pr)
        raise


def _evaluate_head(
    repo: str, pr: int, sha: str, meta: dict, now: datetime
) -> str:
    branch = meta["head"]["ref"]
    body = meta.get("body") or ""
    ecosystem = ecosystem_from_branch(branch)
    if ecosystem is None:
        # An ungated ecosystem (none configured today): don't wedge the PR on a
        # required context we cannot evaluate.
        set_status(repo, sha, "success", f"ecosystem not gated ({branch}).")
        remove_label(repo, pr)
        return f"skipped: unrecognized branch {branch}"

    commits = _gh_json(f"repos/{repo}/pulls/{pr}/commits")
    messages = [c["commit"]["message"] for c in commits]

    if is_security_update(messages, body):
        set_status(repo, sha, "success", "Security update -- cooldown bypassed.")
        remove_label(repo, pr)
        return "pass: security update"

    packages = parse_updated_dependencies(messages)
    if not packages:
        raise CooldownError(f"PR #{pr}: no updated-dependencies trailer found")

    offenders: list[tuple[str, str, datetime]] = []
    unknown: list[str] = []
    for name, version in packages:
        try:
            released = release_date(ecosystem, name, version)
        except CooldownError as exc:
            unknown.append(str(exc))
            continue
        if (now - released).days < COOLDOWN_DAYS:
            offenders.append((name, version, released))

    if unknown:
        # Fail closed: an undeterminable release date must not silently pass a
        # security gate. The daily re-run clears a transient registry blip.
        set_status(repo, sha, "failure", unknown[0])
        add_label(repo, pr)
        return f"block (unknown): {unknown[0]}"

    if offenders:
        name, version, released = max(offenders, key=lambda o: o[2])
        message = cooldown_message(name, version, released, now)
        set_status(repo, sha, "failure", message)
        add_label(repo, pr)
        return f"block: {message}"

    set_status(
        repo, sha, "success",
        f"All {len(packages)} release(s) past the {COOLDOWN_DAYS}-day cooldown.",
    )
    remove_label(repo, pr)
    return f"pass: {len(packages)} release(s) aged out"


def evaluate_all_open(repo: str) -> int:
    out = subprocess.run(
        [
            "gh", "pr", "list", "--repo", repo, "--state", "open",
            "--author", "app/dependabot", "--json", "number", "--limit", "100",
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    numbers = [item["number"] for item in json.loads(out)]
    if not numbers:
        print("supply-chain-cooldown: no open Dependabot PRs")
        return 0
    failed = False
    for pr in numbers:
        try:
            print(f"PR #{pr}: {evaluate_pr(repo, pr)}")
        except Exception as exc:  # noqa: BLE001 -- one PR must not abort the sweep
            failed = True
            print(f"PR #{pr}: ERROR {exc}", file=sys.stderr)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int, help="evaluate a single PR number")
    group.add_argument(
        "--all-open", action="store_true", help="evaluate every open Dependabot PR"
    )
    group.add_argument(
        "--self-test", action="store_true", help="run offline logic tests"
    )
    args = parser.parse_args()

    if args.self_test:
        return _self_test()
    repo = _repo()
    if args.all_open:
        return evaluate_all_open(repo)
    try:
        print(f"PR #{args.pr}: {evaluate_pr(repo, args.pr)}")
    except Exception as exc:  # noqa: BLE001 -- status already posted by evaluate_pr
        print(f"PR #{args.pr}: ERROR {exc}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Self-test                                                                   #
# --------------------------------------------------------------------------- #


def _self_test() -> int:  # noqa: C901 -- a flat table of independent assertions
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # Ecosystem from branch.
    check("eco pip", ecosystem_from_branch("dependabot/pip/api/requests-2.31.0"), "pip")
    check("eco npm", ecosystem_from_branch("dependabot/npm_and_yarn/webui/dev-abc"), "npm")
    check(
        "eco actions",
        ecosystem_from_branch("dependabot/github_actions/actions-7a5"),
        "github-actions",
    )
    check(
        "eco gomod",
        ecosystem_from_branch("dependabot/go_modules/relay/production-e"),
        "gomod",
    )
    check("eco docker", ecosystem_from_branch("dependabot/docker/api/python-3.13"), "docker")
    check("eco compose", ecosystem_from_branch("dependabot/docker-compose/docker-abc"), "docker")
    check("eco non-dependabot", ecosystem_from_branch("feature/foo"), None)
    check("eco unknown", ecosystem_from_branch("dependabot/cargo/crate-1.0"), None)

    # Trailer parsing: single (quoted scoped name).
    single = (
        'chore(deps): bump\n\n---\nupdated-dependencies:\n'
        '- dependency-name: "@biomejs/biome"\n  dependency-version: 2.5.4\n'
        "  dependency-type: direct:development\n...\n\n"
        "Signed-off-by: dependabot[bot]"
    )
    check("trailer single", parse_updated_dependencies([single]), [("@biomejs/biome", "2.5.4")])

    # Trailer parsing: grouped (two entries) + a non-Dependabot commit.
    grouped = (
        "chore(deps): bump the docker group\n\n---\nupdated-dependencies:\n"
        "- dependency-name: chrislusf/seaweedfs\n  dependency-version: 4.39\n"
        "  dependency-type: direct:production\n"
        "- dependency-name: cloudflare/cloudflared\n  dependency-version: 2026.7.1\n"
        "  dependency-type: direct:production\n...\n"
    )
    check(
        "trailer grouped",
        parse_updated_dependencies([grouped, "fix: manual tweak\n"]),
        [("chrislusf/seaweedfs", "4.39"), ("cloudflare/cloudflared", "2026.7.1")],
    )

    # De-duplication across commits.
    check("trailer dedupe", parse_updated_dependencies([single, single]), [("@biomejs/biome", "2.5.4")])

    # Security detection.
    ghsa_commit = "chore(deps): bump lib\n\nFixes GHSA-abcd-ef12-3456.\n"
    check("sec ghsa-commit", is_security_update([ghsa_commit], ""), True)
    check("sec legacy-token", is_security_update(["... dependabot_security_updates ..."], ""), True)
    check("sec ghsa-body", is_security_update([""], "Bumps foo.\nSee GHSA-abcd-ef12-3456."), True)
    check("sec body-heading", is_security_update([""], "## Security\nfixes a CVE"), True)
    check(
        "sec release-notes-only",  # upstream notes below <details> must not spoof
        is_security_update(
            [""],
            "Bumps foo.\n<details>\n<summary>Release notes</summary>\n"
            "<h2>Security</h2>\nSee GHSA-abcd-ef12-3456\n",
        ),
        False,
    )
    check("sec none", is_security_update(["chore(deps): bump foo"], "Bumps foo from 1 to 2."), False)

    # Registry-address helpers.
    check("gomod escape", _gomod_escape("github.com/Azure/go-ansiterm"), "github.com/!azure/go-ansiterm")
    check("go version", _go_version("1.24.0"), "v1.24.0")
    check("go version keep-v", _go_version("v1.24.0"), "v1.24.0")
    check("npm url", _npm_url("@biomejs/biome"), "https://registry.npmjs.org/@biomejs%2Fbiome")
    check("gh tags", _github_tag_candidates("7.0.1"), ["v7.0.1", "7.0.1"])
    check("docker official", parse_docker_ref("python"), ("dockerhub", "library", "python"))
    check("docker namespaced", parse_docker_ref("chrislusf/seaweedfs"), ("dockerhub", "chrislusf", "seaweedfs"))
    check("docker docker.io", parse_docker_ref("docker.io/library/redis"), ("dockerhub", "library", "redis"))
    check("docker ghcr", parse_docker_ref("ghcr.io/astral-sh/uv"), ("ghcr", "astral-sh", "uv"))

    # ISO parsing (Z suffix, fractional seconds).
    check("iso z", parse_iso("2024-01-01T12:00:00Z"), datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc))
    check(
        "iso frac",
        parse_iso("2024-01-01T12:00:00.123456Z"),
        datetime(2024, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc),
    )

    # release_date field extraction, per ecosystem, with injected transports.
    def boom_http(url: str) -> dict:
        raise AssertionError(f"unexpected HTTP call: {url}")

    def boom_gh(path: str) -> dict:
        raise AssertionError(f"unexpected gh call: {path}")

    def http_stub(payload: dict, expect: str) -> Transport:
        def transport(url: str) -> dict:
            assert expect in url, f"expected {expect!r} in {url!r}"
            return payload
        return transport

    def gh_stub(path_map: dict[str, dict]) -> Transport:
        def transport(path: str) -> dict:
            for key, value in path_map.items():
                if key in path:
                    return value
            raise subprocess.CalledProcessError(1, ["gh", "api", path])
        return transport

    def rd(eco: str, name: str, ver: str, **kw: object) -> datetime:
        return release_date(eco, name, ver, **kw)  # type: ignore[arg-type]

    check(
        "rd pip",
        rd("pip", "requests", "2.31.0",
           http_get=http_stub(
               {"releases": {"2.31.0": [{"upload_time_iso_8601": "2024-05-20T10:00:00.000000Z"}]}},
               "pypi.org/pypi/requests/json"),
           gh_get=boom_gh),
        datetime(2024, 5, 20, 10, tzinfo=timezone.utc),
    )
    check(
        "rd npm",
        rd("npm", "@biomejs/biome", "2.5.4",
           http_get=http_stub({"time": {"2.5.4": "2026-07-15T08:30:00.000Z"}}, "@biomejs%2Fbiome"),
           gh_get=boom_gh),
        datetime(2026, 7, 15, 8, 30, tzinfo=timezone.utc),
    )
    check(
        "rd gomod",
        rd("gomod", "github.com/prometheus/client_golang", "1.24.0",
           http_get=http_stub({"Time": "2025-01-02T03:04:05Z"}, "/@v/v1.24.0.info"),
           gh_get=boom_gh),
        datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    check(
        "rd github-actions",  # v-prefixed tag tried first
        rd("github-actions", "actions/checkout", "7.0.1",
           http_get=boom_http,
           gh_get=gh_stub({"repos/actions/checkout/releases/tags/v7.0.1": {"published_at": "2025-06-01T00:00:00Z"}})),
        datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    check(
        "rd docker hub",  # official image -> library/*, Docker Hub transport only
        rd("docker", "python", "3.13-slim",
           http_get=http_stub({"tag_last_pushed": "2026-07-01T00:00:00Z"},
                              "hub.docker.com/v2/repositories/library/python/tags/3.13-slim"),
           gh_get=boom_gh),
        datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    check(
        "rd docker ghcr",  # ghcr.io -> GitHub Releases, never Docker Hub
        rd("docker", "ghcr.io/astral-sh/uv", "0.11.28",
           http_get=boom_http,
           gh_get=gh_stub({"repos/astral-sh/uv/releases/tags/0.11.28": {"published_at": "2026-07-07T23:14:13Z"}})),
        datetime(2026, 7, 7, 23, 14, 13, tzinfo=timezone.utc),
    )

    # An unsupported registry fails closed rather than fabricating a URL.
    try:
        parse_docker_ref("quay.io/prometheus/busybox")
        failures.append("quay.io: expected CooldownError, got none")
    except CooldownError:
        pass

    # Cooldown message.
    released = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc)
    check(
        "message",
        cooldown_message("requests", "2.31.0", released, now),
        "requests@2.31.0 was released 3 days ago; 7-day cooldown requires waiting until 2026-07-27.",
    )
    # Boundary: exactly 7 days old is NOT blocked (< COOLDOWN_DAYS).
    check("boundary 7d", (now - datetime(2026, 7, 16, tzinfo=timezone.utc)).days < COOLDOWN_DAYS, False)
    check("boundary 6d", (now - datetime(2026, 7, 17, tzinfo=timezone.utc)).days < COOLDOWN_DAYS, True)

    if failures:
        print("supply_chain_cooldown --self-test FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("supply_chain_cooldown --self-test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
