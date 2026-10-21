"""Refresh the Python lock with the repository's dependency-update policy."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path


def refresh(
    project: Path,
    *,
    upgrade: bool = False,
    security: bool = False,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=7)
    command = [
        "uv",
        "lock",
        "--exclude-newer",
        "false" if security else cutoff.isoformat(),
    ]
    if not security:
        locked = tomllib.loads((project / "uv.lock").read_text())
        for package in locked["package"]:
            if "registry" not in package["source"]:
                continue
            artifacts = package.get("wheels", []) + [package.get("sdist", {})]
            if any(
                datetime.fromisoformat(artifact["upload-time"]) >= cutoff
                for artifact in artifacts
                if "upload-time" in artifact
            ):
                # Keep an already reviewed fresh security fix, without admitting
                # another young version of this package during routine updates.
                command.extend(
                    [
                        "--upgrade-package",
                        f"{package['name']}=={package['version']}",
                        "--exclude-newer-package",
                        f"{package['name']}=false",
                    ]
                )
    if upgrade:
        command.append("--upgrade")
    subprocess.run(command, cwd=project, check=True)
    lock = tomllib.loads((project / "uv.lock").read_text())
    # CLI cutoffs are stored in uv.lock. Regenerate without them so normal
    # uv sync --locked / lock --check work, retaining exactly this resolution.
    normalize = ["uv", "lock", "--exclude-newer", "false"]
    for package in lock["package"]:
        if "registry" in package["source"]:
            normalize.extend(
                ["--upgrade-package", f"{package['name']}=={package['version']}"]
            )
    subprocess.run(normalize, cwd=project, check=True)


def dependabot_security(repo: str, pr: int) -> bool:
    from supply_chain_cooldown import is_security_update

    endpoint = f"repos/{repo}/pulls/{pr}"
    meta = json.loads(subprocess.check_output(["gh", "api", endpoint], text=True))
    if meta["user"]["login"] != "dependabot[bot]":
        raise ValueError("The helper only accepts Dependabot-authored pull requests")
    pages = json.loads(
        subprocess.check_output(
            ["gh", "api", f"{endpoint}/commits", "--paginate", "--slurp"], text=True
        )
    )
    messages = [commit["commit"]["message"] for page in pages for commit in page]
    return is_security_update(messages, meta.get("body") or "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("api"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--upgrade", action="store_true")
    mode.add_argument("--dependabot-pr", type=int)
    args = parser.parse_args()
    security = bool(args.dependabot_pr) and dependabot_security(
        os.environ["GITHUB_REPOSITORY"], args.dependabot_pr
    )
    refresh(args.project, upgrade=args.upgrade, security=security)


if __name__ == "__main__":
    main()
