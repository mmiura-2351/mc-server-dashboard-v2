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
   Validates the target versions against changed manifests read as data at the
   immutable merge-base and head revisions; a mismatch fails closed.
2. Adds newly adopted resolved npm/uv.lock packages, including transitives, and
   looks up each release's publish date in the upstream registry.
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
import base64
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    "uv": "pip",
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


def qualify_docker_image(image: str, sources: Callable[[], list[str]]) -> str:
    """Restore the registry host Dependabot's commit trailer drops (issue #2506).

    ``updated-dependencies`` names an image without its registry, so
    ``ghcr.io/astral-sh/uv`` arrives as ``astral-sh/uv`` and parse_docker_ref
    below would fall through to Docker Hub -- where uv does not exist -- and
    block a release that is in fact months old (PR #2365). The host is therefore
    recovered from the image's own pin in ``sources()`` (the ``FROM`` / ``image:``
    lines). PR evaluation supplies the changed head manifests read through the
    API. ``sources`` is a callable so an already-qualified name costs no read.

    Matching is a name-anchored search rather than Dockerfile/compose parsing --
    both file kinds write ``[host/]name[:tag|@digest]`` the same way, and the
    leading boundary keeps a longer name that merely ends in ``name`` from
    matching. A name pinned under two different hosts is ambiguous and raises
    (fail closed); no match at all leaves the name alone, i.e. Docker Hub, which
    is both the correct answer for a genuine Docker Hub image and a blocking one
    for anything else.
    """
    first = image.split("/", 1)[0]
    if "." in first or ":" in first:
        return image  # Already host-qualified; the tree has nothing to add.
    pattern = re.compile(
        r"(?m)(?:^|[\s\"'=])"
        r"(?:([a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?)/)?"
        + re.escape(image)
        + r"(?=[:@\s\"']|$)"
    )
    hosts = {m.group(1) or "" for text in sources() for m in pattern.finditer(text)}
    if len(hosts) > 1:
        raise CooldownError(f"ambiguous registry for {image}: {sorted(hosts)}")
    host = hosts.pop() if hosts else ""
    return f"{host}/{image}" if host else image


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

    A name taken from a Dependabot trailer must go through qualify_docker_image
    first: the trailer strips the host, and an unqualified name silently means
    Docker Hub here.
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


def _repo_image_sources() -> list[str]:
    """Text of every file that could pin a container image, for the host lookup.

    This is the fallback for standalone release-date callers. PR evaluation
    first qualifies names from immutable head manifests read through the API.
    Under ``pull_request_target`` the local checkout is the trusted BASE branch,
    never the PR head.
    """
    root = Path(__file__).resolve().parent.parent
    # ``.claude`` holds agent worktrees -- whole extra copies of the repo, whose
    # pins would read as a second host for the same image. Tested relative to
    # root, never on the absolute path: a checkout can itself sit under one of
    # these names, and matching absolutely would then skip the entire tree.
    skip = {".git", ".claude", "node_modules", ".venv"}
    texts: list[str] = []
    for pattern in ("Dockerfile*", "*compose*.yaml", "*compose*.yml"):
        for path in sorted(root.rglob(pattern)):
            if skip & set(path.relative_to(root).parts):
                continue
            try:
                texts.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue  # Unreadable file -- a missing host blocks, so this is safe.
    return texts


def _pr_manifests(repo: str, pr: int, meta: dict, ecosystem: str) -> list[tuple[str, str, str]]:
    """Read changed manifests as data at immutable merge-base/head revisions."""
    files: list[dict] = []
    page = 1
    while True:
        batch = _gh_json(f"repos/{repo}/pulls/{pr}/files?per_page=100&page={page}")
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if len(files) != meta["changed_files"]:
        raise CooldownError("incomplete PR file listing")
    base = _gh_json(f"repos/{repo}/compare/{meta['base']['sha']}...{meta['head']['sha']}")
    base_sha = base["merge_base_commit"]["sha"]

    def read(source_repo: str, path: str, sha: str) -> str:
        data = _gh_json(f"repos/{source_repo}/contents/{urllib.parse.quote(path)}?ref={sha}")
        if data.get("type") != "file" or data.get("encoding") != "base64":
            raise CooldownError(f"cannot read complete manifest: {path}")
        return base64.b64decode(data["content"]).decode("utf-8")

    manifests: list[tuple[str, str, str]] = []
    for item in files:
        path = item["filename"]
        filename = path.rsplit("/", 1)[-1]
        relevant = {
            "docker": filename.startswith("Dockerfile") or filename in ("compose.yaml", "compose.yml"),
            "pip": filename in ("pyproject.toml", "uv.lock"),
            "npm": filename == "package-lock.json",
            "gomod": filename == "go.mod",
            "github-actions": path.startswith(".github/workflows/") and filename.endswith((".yml", ".yaml")),
        }[ecosystem]
        if not relevant or item["status"] == "removed":
            continue
        before = "" if item["status"] == "added" else read(
            repo, item.get("previous_filename", path), base_sha)
        after = read(meta["head"]["repo"]["full_name"], path, meta["head"]["sha"])
        manifests.append((path, before, after))
    return manifests


def _manifest_pins(ecosystem: str, text: str, name: str) -> dict[str, str]:
    """Read pins for one dependency in the repository's current manifest formats."""
    if ecosystem == "docker":
        refs = re.findall(r"(?im)^\s*(?:FROM\s+(?:--platform=\S+\s+)?|image:\s*)[\"']?([^\s\"']+)", text)
        versions = []
        for ref in refs:
            image, separator, version = ref.rpartition(":")
            first = image.split("/", 1)[0]
            bare = image.split("/", 1)[1] if "/" in image and ("." in first or ":" in first) else image
            if separator and name in (image, bare):
                versions.append(version.split("@", 1)[0])
        return {str(index): version for index, version in enumerate(versions)}
    if ecosystem == "npm":
        if not text:
            return {}
        return {key: item["version"] for key, item in json.loads(text)["packages"].items()
                if (key == f"node_modules/{name}" or key.endswith(f"/node_modules/{name}"))
                and "version" in item}
    if ecosystem == "pip":
        canonical = lambda value: re.sub(r"[-_.]+", "-", value).lower()
        versions = [item["version"] for item in tomllib.loads(text).get("package", [])
                    if canonical(item["name"]) == canonical(name) and "version" in item]
        return {str(index): version for index, version in enumerate(versions)}
    if ecosystem == "gomod":
        versions = re.findall(r"(?m)^\s*(?:require\s+)?" + re.escape(name) + r"\s+(v\S+)", text)
        return {str(index): version.removeprefix("v") for index, version in enumerate(versions)}
    if ecosystem == "github-actions":
        refs = re.findall(r"(?m)^\s*(?:-\s*)?uses:\s*[\"']?" + re.escape(name) + r"@([^\s#\"']+)", text)
        return {str(index): ref for index, ref in enumerate(refs)}
    raise CooldownError(f"unsupported manifest validation: {ecosystem}")


def _pip_requirements(text: str, name: str) -> set[str]:
    data = tomllib.loads(text)
    project = data.get("project", {})
    entries = list(project.get("dependencies", []))
    for group in (*project.get("optional-dependencies", {}).values(),
                  *data.get("dependency-groups", {}).values()):
        entries.extend(group)
    specs = set()
    for entry in entries:
        match = re.fullmatch(r"([\w.-]+)(?:\[[^]]+\])?\s*(.+)", entry) if isinstance(entry, str) else None
        if match and re.sub(r"[-_.]+", "-", match[1]).lower() == re.sub(r"[-_.]+", "-", name).lower():
            specs.add(match[2])
    return specs


def _numeric_version(version: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        raise CooldownError(f"unsupported requirement version: {version}")
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _requirement_admits(spec: str, version: str) -> bool:
    target = _numeric_version(version)
    admitted = True
    for clause in spec.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|!=|<|>)\s*(\d+(?:\.\d+)*)\s*", clause)
        if not match:
            raise CooldownError(f"unsupported requirement constraint: {spec}")
        bound = _numeric_version(match[2])
        admitted &= {">=": target >= bound, "<=": target <= bound, "==": target == bound,
                     "!=": target != bound, "<": target < bound, ">": target > bound}[match[1]]
    return admitted


def _pip_requirement_update(name: str, version: str, manifests: list[tuple[str, str, str]]) -> bool:
    for path, before, after in manifests:
        if not path.endswith("/pyproject.toml"):
            continue
        old, new = _pip_requirements(before, name), _pip_requirements(after, name)
        if old == new:
            continue
        for spec in new - old:
            if not _requirement_admits(spec, version):
                continue
            # A raised floor names the target; a widened cap newly admits it.
            bounds = {_numeric_version(bound) for bound in re.findall(r"(?:>=|==)\s*(\d+(?:\.\d+)*)", spec)}
            old_bounds = {_numeric_version(bound) for previous in old
                          for bound in re.findall(r"(?:>=|==)\s*(\d+(?:\.\d+)*)", previous)}
            if _numeric_version(version) in bounds - old_bounds or not any(
                _requirement_admits(previous, version) for previous in old
            ):
                return True
    return False


def validate_updated_dependencies(
    ecosystem: str, packages: list[tuple[str, str]], manifests: list[tuple[str, str, str]]
) -> None:
    """Reject trailers that disagree with the changed dependency pins."""
    for name, version in packages:
        adopted: set[str] = set()
        for path, before, after in manifests:
            if ecosystem == "pip" and path.endswith("/pyproject.toml"):
                continue
            old = _manifest_pins(ecosystem, before, name)
            new = _manifest_pins(ecosystem, after, name)
            adopted.update(value for key, value in new.items() if old.get(key) != value)
        if not adopted and ecosystem == "pip" and _pip_requirement_update(name, version, manifests):
            continue
        target = version.removeprefix("v") if ecosystem == "gomod" else version
        if ecosystem == "github-actions" and not adopted.intersection(_github_tag_candidates(version)):
            for tag in _github_tag_candidates(version):
                try:
                    target = _gh_json(f"repos/{name}/commits/{urllib.parse.quote(tag, safe='')}")["sha"]
                    break
                except subprocess.CalledProcessError:
                    continue
        if adopted != {target} and not (
            ecosystem == "github-actions" and len(adopted) == 1 and
            adopted.intersection(_github_tag_candidates(version))
        ):
            raise CooldownError(f"{name}: trailer {version} disagrees with adopted versions {sorted(adopted)}")


def _lockfile_packages(ecosystem: str, text: str) -> set[tuple[str, str, str]]:
    """Read external name/version/source identities without trusting lock dates."""
    if not text:
        return set()
    packages = set()
    if ecosystem == "npm":
        data = json.loads(text)
        if data.get("lockfileVersion") not in (2, 3) or not isinstance(data.get("packages"), dict):
            raise CooldownError("unsupported npm lockfile format")
        local_paths = {item["resolved"] for item in data["packages"].values() if item.get("link")}
        for path, item in data["packages"].items():
            if not path or (path in local_paths and "node_modules/" not in path):
                continue
            source = item.get("resolved", "")
            location = urllib.parse.urlsplit(source)
            if (item.get("link") and not location.scheme and not location.netloc) or (
                location.scheme == "file" and not location.netloc
            ):
                continue
            if "node_modules/" not in path:
                raise CooldownError(f"unsupported npm package path: {path}")
            name = item.get("name", path.rsplit("node_modules/", 1)[1])
            version = item.get("version")
            if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
                raise CooldownError(f"missing npm package identity: {path}")
            packages.add((name, version, source))
    else:
        data = tomllib.loads(text)
        if data.get("version") != 1 or not isinstance(data.get("package"), list):
            raise CooldownError("unsupported uv lockfile format")
        for item in data["package"]:
            source = item.get("source", {})
            if len(source) == 1 and set(source) & {"editable", "virtual", "directory", "path"}:
                location = urllib.parse.urlsplit(next(iter(source.values())))
                if not location.scheme and not location.netloc:
                    continue
            name, version = item.get("name"), item.get("version")
            if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
                raise CooldownError("missing uv package identity")
            registry = source["registry"] if set(source) == {"registry"} else json.dumps(source, sort_keys=True)
            packages.add((re.sub(r"[-_.]+", "-", name).lower(), version, registry))
    return packages


def newly_resolved_dependencies(ecosystem: str, manifests: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Include every newly adopted external version in changed npm/uv locks."""
    if ecosystem not in ("npm", "pip"):
        return []
    old: set[tuple[str, str, str]] = set()
    new: set[tuple[str, str, str]] = set()
    for path, before, after in manifests:
        if path.rsplit("/", 1)[-1] != ("package-lock.json" if ecosystem == "npm" else "uv.lock"):
            continue
        old.update(_lockfile_packages(ecosystem, before))
        new.update(_lockfile_packages(ecosystem, after))
    adopted = set()
    previous_versions = {(name, version) for name, version, _ in old}
    for name, version, source in new - old:
        location = urllib.parse.urlsplit(source)
        if ecosystem == "npm":
            public_source = (location.scheme == "https" and location.netloc == "registry.npmjs.org" and
                             urllib.parse.unquote(location.path).startswith(f"/{name}/-/"))
        else:
            public_source = source.rstrip("/") == "https://pypi.org/simple"
        if not public_source:
            raise CooldownError(f"cannot verify registry source for {name}@{version}: {source}")
        if (name, version) not in previous_versions:
            adopted.add((name, version))
    return sorted(adopted)


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
    image_sources: Callable[[], list[str]] = _repo_image_sources,
) -> datetime:
    """Upstream publish date for name@version, per docs/dev/DEPENDENCIES.md.

    ``http_get`` / ``gh_get`` / ``image_sources`` are injected so the
    per-ecosystem field extraction (the part that historically hid a bug) and the
    docker registry-host recovery are exercised offline in --self-test.
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
            registry, owner, repo = parse_docker_ref(
                qualify_docker_image(name, image_sources)
            )
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
    sha = meta["head"]["sha"]
    if meta["user"]["login"] != BOT:
        # A human PR adopts no Dependabot-managed pin, so there is nothing to
        # age -- but the context is REQUIRED, and a required status that is
        # never posted wedges the PR forever on a check that cannot arrive.
        # Post the pass explicitly so the required context is satisfied.
        set_status(repo, sha, "success", "Not a Dependabot PR -- cooldown not applicable.")
        return "skipped: not a Dependabot PR"
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
    commits = _gh_json(f"repos/{repo}/pulls/{pr}/commits")
    messages = [c["commit"]["message"] for c in commits]

    if is_security_update(messages, body):
        set_status(repo, sha, "success", "Security update -- cooldown bypassed.")
        remove_label(repo, pr)
        return "pass: security update"

    ecosystem = ecosystem_from_branch(branch)
    if ecosystem is None:
        raise CooldownError(f"unrecognized Dependabot ecosystem branch: {branch}")

    packages = parse_updated_dependencies(messages)
    if not packages:
        raise CooldownError(f"PR #{pr}: no updated-dependencies trailer found")

    manifests = _pr_manifests(repo, pr, meta, ecosystem)
    validate_updated_dependencies(ecosystem, packages, manifests)
    if ecosystem == "pip":
        packages = [(re.sub(r"[-_.]+", "-", name).lower(), version) for name, version in packages]
    packages = list(dict.fromkeys([*packages, *newly_resolved_dependencies(ecosystem, manifests)]))

    offenders: list[tuple[str, str, datetime]] = []
    unknown: list[str] = []
    for name, version in packages:
        try:
            lookup_name = qualify_docker_image(name, lambda: [after for _, _, after in manifests]) if ecosystem == "docker" else name
            released = release_date(ecosystem, lookup_name, version)
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
    check("eco uv", ecosystem_from_branch("dependabot/uv/api/ruff-0.17.1"), "pip")
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

    def boom_sources() -> list[str]:
        raise AssertionError("unexpected image-source read")

    # Registry-host recovery from the pins in the checked-out tree. Fixtures, not
    # the real tree, so --self-test stays offline and independent of it.
    dockerfile = (
        "FROM ghcr.io/astral-sh/uv:0.11.30 AS uv\n"
        "FROM node:24-slim AS webui\n"
        "FROM python:3.13-slim AS builder\n"
    )
    compose = "services:\n  seaweedfs:\n    image: chrislusf/seaweedfs:4.40\n"
    check("qualify ghcr", qualify_docker_image("astral-sh/uv", lambda: [dockerfile]), "ghcr.io/astral-sh/uv")
    check("qualify official", qualify_docker_image("python", lambda: [dockerfile]), "python")
    check("qualify compose", qualify_docker_image("chrislusf/seaweedfs", lambda: [compose]), "chrislusf/seaweedfs")
    check("qualify absent", qualify_docker_image("cloudflare/cloudflared", lambda: [dockerfile]), "cloudflare/cloudflared")
    check("qualify already-hosted", qualify_docker_image("ghcr.io/astral-sh/uv", boom_sources), "ghcr.io/astral-sh/uv")

    # Two hosts for one name is unresolvable -- block rather than guess.
    try:
        qualify_docker_image("astral-sh/uv", lambda: [dockerfile, "FROM docker.io/astral-sh/uv:1\n"])
        failures.append("qualify ambiguous: expected CooldownError, got none")
    except CooldownError:
        pass

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
           gh_get=boom_gh, image_sources=lambda: [dockerfile]),
        datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    check(
        "rd docker ghcr",  # ghcr.io -> GitHub Releases, never Docker Hub
        rd("docker", "ghcr.io/astral-sh/uv", "0.11.28",
           http_get=boom_http, image_sources=boom_sources,  # already qualified -> no tree read
           gh_get=gh_stub({"repos/astral-sh/uv/releases/tags/0.11.28": {"published_at": "2026-07-07T23:14:13Z"}})),
        datetime(2026, 7, 7, 23, 14, 13, tzinfo=timezone.utc),
    )
    check(
        "rd docker ghcr bare",  # issue #2506: the trailer's host-less name must still reach ghcr
        rd("docker", "astral-sh/uv", "0.11.30",
           http_get=boom_http, image_sources=lambda: [dockerfile],
           gh_get=gh_stub({"repos/astral-sh/uv/releases/tags/0.11.30": {"published_at": "2026-07-20T20:48:06Z"}})),
        datetime(2026, 7, 20, 20, 48, 6, tzinfo=timezone.utc),
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

    # A human PR must still get the required status POSTED. Returning without
    # posting leaves the context unset forever, and since it is required in
    # branch protection that wedges the PR permanently -- the failure mode this
    # pass-through exists to prevent. Swap the two module-level GitHub calls
    # (evaluate_pr resolves both through globals at call time).
    posted: list[tuple[str, str, str]] = []
    saved = (globals()["_gh_json"], globals()["set_status"])
    globals()["_gh_json"] = lambda path: {"user": {"login": "octocat"}, "head": {"sha": "cafe1234"}}
    globals()["set_status"] = lambda repo, sha, state, description: posted.append((sha, state, description))
    try:
        outcome = evaluate_pr("owner/repo", 1)
    finally:
        globals()["_gh_json"], globals()["set_status"] = saved
    check("human PR outcome", outcome, "skipped: not a Dependabot PR")
    check("human PR status posted", [(s, st) for s, st, _ in posted], [("cafe1234", "success")])

    def validation_case(label: str, eco: str, packages: list[tuple[str, str]],
                        manifests: list[tuple[str, str, str]], allowed: bool) -> None:
        try:
            validate_updated_dependencies(eco, packages, manifests)
            got = True
        except CooldownError:
            got = False
        check(label, got, allowed)

    validation_case("matching grouped docker", "docker", [("astral-sh/uv", "0.13.1"), ("python", "3.13-slim")],
                    [("api/Dockerfile", "FROM ghcr.io/astral-sh/uv:0.12.17\nFROM python:3.12-slim\n",
                      "FROM ghcr.io/astral-sh/uv:0.13.1\nFROM python:3.13-slim\n")], True)
    validation_case("matching compose", "docker", [("postgres", "18.6")],
                    [("compose.yaml", "  image: postgres:18.5\n", "  image: postgres:18.6\n")], True)
    validation_case("matching npm nested lock", "npm", [("@scope/pkg", "2.0.0")],
                    [("webui/package-lock.json", '{"packages":{"node_modules/@scope/pkg":{"version":"1.0.0"}}}',
                      '{"packages":{"node_modules/@scope/pkg":{"version":"2.0.0"}}}')], True)

    validation_case("pip widened cap without lock movement", "pip", [("mypy", "2.4.0")],
                    [("api/pyproject.toml", '[dependency-groups]\ndev = ["mypy>=2.1.0,<2.4"]',
                      '[dependency-groups]\ndev = ["mypy>=2.1.0,<2.5"]')], True)
    validation_case("pip stale cap target", "pip", [("mypy", "2.3.0")],
                    [("api/pyproject.toml", '[dependency-groups]\ndev = ["mypy>=2.1.0,<2.4"]',
                      '[dependency-groups]\ndev = ["mypy>=2.1.0,<2.5"]')], False)
    lock_before = '[[package]]\nname = "mypy"\nversion = "2.1.0"\n'
    lock_after = '[[package]]\nname = "mypy"\nversion = "2.4.0"\n'
    validation_case("pip regenerated lock", "pip", [("mypy", "2.4.0")],
                    [("api/uv.lock", lock_before, lock_after)], True)
    validation_case("pip stale regenerated lock", "pip", [("mypy", "2.3.0")],
                    [("api/uv.lock", lock_before, lock_after)], False)
    validation_case("pip raised floor", "pip", [("some_pkg", "2.4.0")],
                    [("api/pyproject.toml", '[project]\ndependencies = ["Some-Pkg[extra]>=2.1.0,<3"]',
                      '[project]\ndependencies = ["Some-Pkg[extra]>=2.4.0,<3"]')], True)
    validation_case("gomod grouped module", "gomod", [("golang.org/x/sys", "0.49.0")],
                    [("worker/go.mod", "require (\n golang.org/x/sys v0.48.0 // indirect\n)",
                      "require (\n golang.org/x/sys v0.49.0 // indirect\n)")], True)
    validation_case("gomod standalone module", "gomod", [("golang.org/x/sys", "v0.49.0")],
                    [("worker/go.mod", "require golang.org/x/sys v0.48.0\n",
                      "require golang.org/x/sys v0.49.0\n")], True)
    validation_case("pip lock disagreement cannot fall back to broad requirement", "pip", [("mypy", "2.3.0")],
                    [("api/uv.lock", lock_before, lock_after),
                     ("api/pyproject.toml", '[dependency-groups]\ndev = ["mypy>=2.1.0,<2.3"]',
                      '[dependency-groups]\ndev = ["mypy>=2.1.0,<2.5"]')], False)
    validation_case("unsupported pip constraint fails closed", "pip", [("mypy", "2.4.0rc1")],
                    [("api/pyproject.toml", '[dependency-groups]\ndev = ["mypy>=2.1.0,<2.4"]',
                      '[dependency-groups]\ndev = ["mypy>=2.1.0,<2.5"]')], False)
    validation_case("missing adopted pin fails closed", "docker", [("python", "3.13-slim")], [], False)
    saved_gh = globals()["_gh_json"]
    globals()["_gh_json"] = lambda path: {"sha": "b" * 40}
    try:
        action_manifests = [(".github/workflows/test.yml", "- uses: actions/checkout@" + "a" * 40,
                             "- uses: actions/checkout@" + "b" * 40 + " # v6.0.0")]
        validation_case("action verifies SHA instead of comment", "github-actions", [("actions/checkout", "7.0.1")],
                        action_manifests, True)
        globals()["_gh_json"] = lambda path: {"sha": "a" * 40}
        validation_case("action rejects stale target SHA", "github-actions", [("actions/checkout", "6.0.0")],
                        action_manifests, False)
    finally:
        globals()["_gh_json"] = saved_gh
    validation_case("npm adopts version already pinned elsewhere", "npm", [("pkg", "2.0.0")],
                    [("webui/package-lock.json", '{"packages":{"node_modules/pkg":{"version":"1.0.0"},"node_modules/other/node_modules/pkg":{"version":"2.0.0"}}}',
                      '{"packages":{"node_modules/pkg":{"version":"2.0.0"},"node_modules/other/node_modules/pkg":{"version":"2.0.0"}}}')], True)
    validation_case("pip unchanged floor cannot validate stale cap target", "pip", [("mypy", "2.1.0")],
                    [("api/pyproject.toml", '[dependency-groups]\ndev = ["mypy>=2.1.0,<2.4"]',
                      '[dependency-groups]\ndev = ["mypy>=2.1.0,<2.5"]')], False)

    def npm_lock(entries: dict[str, dict]) -> str:
        return json.dumps({"lockfileVersion": 3, "packages": entries})

    def npm_package(name: str, version: str) -> dict:
        return {"version": version, "resolved": f"https://registry.npmjs.org/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"}

    def uv_package(name: str, version: str, source: str = 'registry = "https://pypi.org/simple"') -> str:
        return f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ {source} }}\n'

    def resolved_case(label: str, eco: str, before: str, after: str, expected: object) -> None:
        filename = "webui/package-lock.json" if eco == "npm" else "api/uv.lock"
        try:
            got = newly_resolved_dependencies(eco, [(filename, before, after)])
        except CooldownError:
            got = "blocked"
        check(label, got, expected)

    npm_before = npm_lock({"node_modules/pkg": npm_package("pkg", "1.0.0"),
                           "node_modules/other/node_modules/pkg": npm_package("pkg", "2.0.0")})
    resolved_case("npm removed and relocated versions are not new", "npm", npm_before,
                  npm_lock({"node_modules/pkg": npm_package("pkg", "2.0.0")}), [])
    resolved_case("npm nested additional version is checked", "npm", npm_before,
                  npm_lock({"node_modules/pkg": npm_package("pkg", "1.0.0"),
                            "node_modules/other/node_modules/pkg": npm_package("pkg", "3.0.0")}), [("pkg", "3.0.0")])
    resolved_case("npm scoped alias uses registry name", "npm", "",
                  npm_lock({"node_modules/alias": {**npm_package("@scope/pkg", "2.0.0"), "name": "@scope/pkg"}}),
                  [("@scope/pkg", "2.0.0")])
    resolved_case("npm root workspace and local archives are excluded", "npm", "", npm_lock({
        "": {"version": "0.0.0"}, "packages/local": {"name": "local", "version": "1.0.0"},
        "node_modules/local": {"resolved": "packages/local", "link": True},
        "node_modules/archive": {"version": "1.0.0", "resolved": "file:../archive.tgz"}}), [])
    resolved_case("npm missing source fails closed", "npm", "",
                  npm_lock({"node_modules/pkg": {"version": "1.0.0"}}), "blocked")
    resolved_case("npm legacy lock fails closed", "npm", "", '{"lockfileVersion": 1, "dependencies": {}}', "blocked")
    resolved_case("npm public artifact URL changes do not recheck an existing release", "npm",
                  npm_lock({"node_modules/pkg": npm_package("pkg", "1.0.0")}),
                  npm_lock({"node_modules/pkg": {**npm_package("pkg", "1.0.0"),
                                                "resolved": "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz?mirror=1"}}), [])
    uv_before = "version = 1\n" + uv_package("Some_Pkg", "1.0.0") + uv_package("other", "2.0.0")
    resolved_case("uv canonical unchanged and removed versions are not new", "pip", uv_before,
                  "version = 1\n" + uv_package("some.pkg", "1.0.0"), [])
    resolved_case("uv duplicate marker resolutions are checked once", "pip", uv_before,
                  "version = 1\n" + uv_package("some-pkg", "1.0.0") + uv_package("other", "3.0.0") * 2,
                  [("other", "3.0.0")])
    resolved_case("uv local packages are excluded", "pip", "", "version = 1\n" + "".join(
        uv_package("local", "0.0.0", f'{kind} = "."') for kind in ("editable", "virtual", "directory", "path")), [])
    resolved_case("uv missing source fails closed", "pip", "", 'version = 1\n[[package]]\nname = "pkg"\nversion = "1"\n', "blocked")
    resolved_case("uv unsupported lock fails closed", "pip", "", "version = 2\npackage = []", "blocked")
    resolved_case("uv equivalent public source does not recheck an existing release", "pip",
                  "version = 1\n" + uv_package("pkg", "1.0.0"),
                  "version = 1\n" + uv_package("pkg", "1.0.0", 'registry = "https://pypi.org/simple/"'), [])
    resolved_case("uv changed unsupported source fails closed", "pip",
                  "version = 1\n" + uv_package("pkg", "1.0.0", 'git = "https://example.com/old"'),
                  "version = 1\n" + uv_package("pkg", "1.0.0", 'git = "https://example.com/new"'), "blocked")
    # Exercise the real status-posting path with immutable API manifest fixtures.
    saved = {key: globals()[key] for key in
             ("_gh_json", "set_status", "add_label", "remove_label", "release_date")}
    requests: list[str] = []
    lookups: list[tuple[str, str]] = []
    trailer_version = "0.12.23"
    branch = "dependabot/docker/api/docker-group"
    security = False
    incomplete = False
    paginate = False
    graph_manifests: dict[str, tuple[str, str]] | None = None
    missing_publish_date = False

    def pr_fixture(path: str) -> object:
        requests.append(path)
        python_update = branch.startswith(("dependabot/uv/", "dependabot/pip/"))
        if path.endswith("/commits"):
            name = "ruff" if python_update else "@scope/direct" if graph_manifests is not None else "astral-sh/uv"
            trailer = (f"updated-dependencies:\n- dependency-name: {name}\n"
                       f"  dependency-version: {trailer_version}\n...")
            return [{"commit": {"message": trailer}},
                    {"commit": {"message": "Regenerate lockfile" + (" GHSA-abcd-ef12-3456" if security else "")}}]
        if "/files?" in path:
            if graph_manifests is not None:
                return [{"filename": filename, "status": "modified"} for filename in graph_manifests]
            if paginate and path.endswith("&page=1"):
                return [{"filename": f"ignored/{index}.txt", "status": "modified"} for index in range(100)]
            if python_update:
                return [{"filename": path, "status": "modified"} for path in ("api/pyproject.toml", "api/uv.lock")]
            return [] if incomplete else [{"filename": "api/Dockerfile", "status": "modified"}]
        if "/compare/" in path:
            return {"merge_base_commit": {"sha": "merge-base"}}
        if "/contents/" in path:
            if graph_manifests is not None:
                filename = path.split("/contents/", 1)[1].split("?", 1)[0]
                text = graph_manifests[filename][1 if "ref=head" in path else 0]
                return {"type": "file", "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
            version = "0.13.1" if "ref=head" in path else "0.12.17"
            text = f"FROM ghcr.io/astral-sh/uv:{version} AS uv\n"
            if python_update:
                version = "0.17.1" if "ref=head" in path else "0.17.0"
                text = (f'[dependency-groups]\ndev = ["ruff>={version},<0.18"]\n' if "/pyproject.toml?" in path
                        else f'version = 1\n[[package]]\nname = "ruff"\nversion = "{version}"\n'
                             'source = { registry = "https://pypi.org/simple" }\n')
            return {"type": "file", "encoding": "base64", "content":
                    base64.b64encode(text.encode()).decode()}
        if path == "repos/owner/repo/pulls/3386":
            return {"user": {"login": BOT}, "head": {"sha": "head", "ref":
                    branch, "repo": {"full_name": "owner/repo"}},
                    "base": {"sha": "base"}, "changed_files": len(graph_manifests) if graph_manifests is not None else 2 if python_update else 101 if paginate else 1, "body": ""}
        raise AssertionError(f"unexpected gh call: {path}")

    def fixture_release_date(eco: str, name: str, version: str) -> datetime:
        lookups.append((name, version))
        if graph_manifests is not None:
            stamp = "2026-10-20T00:00:00Z" if name == "transitive-pkg" else "2026-09-01T00:00:00Z"
            payload = ({"time": {version: stamp}} if eco == "npm" else
                       {"releases": {version: [{"upload_time_iso_8601": stamp}]}})
            if missing_publish_date and name == "transitive-pkg":
                payload = {}
            url = _npm_url(name) if eco == "npm" else f"https://pypi.org/pypi/{name}/json"
            return saved["release_date"](eco, name, version, http_get=http_stub(payload, url), gh_get=boom_gh)
        if eco == "pip":
            def pypi_fixture(url: str) -> dict:
                check("uv registry URL", url, "https://pypi.org/pypi/ruff/json")
                return {"releases": {"0.17.1": [{"upload_time_iso_8601": "2026-10-14T05:15:30Z"}]}}
            return saved["release_date"](eco, name, version, http_get=pypi_fixture, gh_get=boom_gh)
        return parse_iso("2026-10-14T05:15:30Z" if version == "0.13.1" else "2026-09-01T00:00:00Z")

    globals()["_gh_json"] = pr_fixture
    globals()["set_status"] = lambda repo, sha, state, description: posted.append((sha, state, description))
    globals()["add_label"] = lambda *args: None
    globals()["remove_label"] = lambda *args: None
    globals()["release_date"] = fixture_release_date
    try:
        branch = "dependabot/cargo/crate-1.0"
        posted.clear()
        requests.clear()
        lookups.clear()
        try:
            evaluate_pr("owner/repo", 3386, now)
        except CooldownError:
            pass
        check("unknown ecosystem posts failure", [(sha, state) for sha, state, _ in posted], [("head", "failure")])
        check("unknown ecosystem skips registry", lookups, [])
        check("unknown ecosystem skips manifests", any("/contents/" in path for path in requests), False)
        security = True
        posted.clear()
        check("unknown ecosystem security exemption", evaluate_pr("owner/repo", 3386, now), "pass: security update")
        check("unknown ecosystem security status", [(sha, state) for sha, state, _ in posted], [("head", "success")])
        branch = "dependabot/docker/api/docker-group"
        for label, trailer_version, security, incomplete, paginate, date, expected in (
            ("stale eligible trailer", "0.12.23", False, False, False, "2026-10-15", "failure"),
            ("matching fresh target plus regeneration commit", "0.13.1", False, False, False, "2026-10-15", "failure"),
            ("matching aged target plus regeneration commit", "0.13.1", False, False, False, "2026-10-22", "success"),
            ("security bypass before manifest reads", "0.12.23", True, False, False, "2026-10-15", "success"),
            ("paginated manifest listing", "0.13.1", False, False, True, "2026-10-22", "success"),
            ("incomplete file listing fails closed", "0.13.1", False, True, False, "2026-10-22", "failure"),
        ):
            posted.clear()
            requests.clear()
            lookups.clear()
            try:
                evaluate_pr("owner/repo", 3386, parse_iso(date + "T00:00:00Z"))
            except CooldownError:
                pass
            check(label + " posted status", [(sha, state) for sha, state, _ in posted], [("head", expected)])
            if security:
                check(label + " reads", any("/contents/" in path for path in requests), False)
            elif not incomplete:
                check(label + " immutable head", "repos/owner/repo/contents/api/Dockerfile?ref=head" in requests, True)
                check(label + " immutable base", "repos/owner/repo/contents/api/Dockerfile?ref=merge-base" in requests, True)
                check(label + " registry targets", lookups,
                      [("ghcr.io/astral-sh/uv", "0.13.1")] if trailer_version == "0.13.1" else [])
        incomplete = paginate = security = False
        for branch, trailer_version, date, expected in (
            ("dependabot/uv/api/ruff-0.17.1", "0.17.1", "2026-10-15", "failure"),
            ("dependabot/uv/api/ruff-0.17.1", "0.17.1", "2026-10-22", "success"),
            ("dependabot/uv/api/ruff-0.17.0", "0.17.0", "2026-10-22", "failure"),
            ("dependabot/pip/api/ruff-0.17.1", "0.17.1", "2026-10-22", "success"),
        ):
            posted.clear()
            requests.clear()
            lookups.clear()
            try:
                evaluate_pr("owner/repo", 3386, parse_iso(date + "T00:00:00Z"))
            except CooldownError:
                pass
            label = f"{branch} at {date}"
            check(label + " status", [(sha, state) for sha, state, _ in posted], [("head", expected)])
            for path in ("api/pyproject.toml", "api/uv.lock"):
                for revision in ("merge-base", "head"):
                    check(label + f" reads {path} at {revision}",
                          f"repos/owner/repo/contents/{path}?ref={revision}" in requests, True)
            check(label + " registry targets", lookups, [("ruff", "0.17.1")] if trailer_version == "0.17.1" else [])

        for branch, filename, before, after in (
            ("dependabot/npm_and_yarn/webui/group", "webui/package-lock.json",
             npm_lock({"": {"name": "webui", "version": "0.0.0"},
                       "node_modules/@scope/direct": npm_package("@scope/direct", "1.0.0")}),
             npm_lock({"": {"name": "webui", "version": "0.0.0"},
                       "node_modules/@scope/direct": npm_package("@scope/direct", "2.0.0"),
                       "node_modules/transitive-pkg": npm_package("transitive-pkg", "3.0.0")})),
            ("dependabot/pip/api/group", "api/uv.lock",
             "version = 1\n" + uv_package("ruff", "1.0.0"),
             "version = 1\n" + uv_package("ruff", "2.0.0") + uv_package("Transitive_Pkg", "3.0.0")),
        ):
            graph_manifests = {filename: (before, after)}
            trailer_version = "2.0.0"
            for date, expected in (("2026-10-22", "failure"), ("2026-10-27", "success")):
                posted.clear()
                lookups.clear()
                requests.clear()
                evaluate_pr("owner/repo", 3386, parse_iso(date + "T00:00:00Z"))
                label = f"{filename} unlisted transitive at {date}"
                check(label + " status", [(sha, state) for sha, state, _ in posted], [("head", expected)])
                check(label + " registry targets", lookups,
                      [("ruff" if "uv.lock" in filename else "@scope/direct", "2.0.0"), ("transitive-pkg", "3.0.0")])
                for revision in ("merge-base", "head"):
                    check(label + " immutable " + revision,
                          f"repos/owner/repo/contents/{filename}?ref={revision}" in requests, True)

            missing_publish_date = True
            posted.clear()
            evaluate_pr("owner/repo", 3386, parse_iso("2026-10-27T00:00:00Z"))
            check(filename + " missing transitive publish date fails closed", posted[-1][1], "failure")
            missing_publish_date = False

            graph_manifests = {filename: (before, after.replace("https://registry.npmjs.org/transitive-pkg", "https://example.com/transitive-pkg")
                                        if filename.endswith(".json") else after.replace(
                                            'name = "Transitive_Pkg"\nversion = "3.0.0"\nsource = { registry = "https://pypi.org/simple" }',
                                            'name = "Transitive_Pkg"\nversion = "3.0.0"\nsource = { git = "https://example.com/transitive-pkg" }'))}
            posted.clear()
            lookups.clear()
            try:
                evaluate_pr("owner/repo", 3386, parse_iso("2026-10-27T00:00:00Z"))
            except CooldownError:
                pass
            check(filename + " unprovable transitive source fails closed", posted[-1][1], "failure")
            check(filename + " unprovable source skips registry", lookups, [])
            security = True
            posted.clear()
            requests.clear()
            check(filename + " security exemption", evaluate_pr("owner/repo", 3386, now), "pass: security update")
            check(filename + " security status", posted[-1][1], "success")
            check(filename + " security skips manifests", any("/contents/" in path for path in requests), False)
            security = False
    finally:
        globals().update(saved)

    if failures:
        print("supply_chain_cooldown --self-test FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("supply_chain_cooldown --self-test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
