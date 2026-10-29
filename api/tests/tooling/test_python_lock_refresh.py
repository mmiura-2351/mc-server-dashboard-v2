"""Exercise the updater against a deterministic package index with real uv."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
import tomllib
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 1, 20, tzinfo=UTC)


@pytest.fixture
def updater(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "refresh_python_lock", ROOT / "scripts/refresh_python_lock.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    files: dict[str, bytes] = {}
    for name in ("direct", "indirect"):
        entries = []
        for version, day in (
            ("1.0", 1),
            ("1.1", 10),
            ("1.2", 19),
            ("1.3", 20),
            ("2.0", 10),
        ):
            filename = f"{name}-{version}-py3-none-any.whl"
            wheel = tmp_path / filename
            info = f"{name}-{version}.dist-info"
            with zipfile.ZipFile(wheel, "w") as archive:
                metadata = (
                    f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n"
                    "Requires-Python: >=3.11\n"
                )
                if name == "direct":
                    metadata += (
                        "Requires-Dist: indirect>=1.2,<2\n"
                        if version == "1.2"
                        else "Requires-Dist: indirect>=1,<2\n"
                    )
                archive.writestr(f"{info}/METADATA", metadata)
                archive.writestr(
                    f"{info}/WHEEL",
                    "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                )
                archive.writestr(f"{info}/RECORD", "")
            data = wheel.read_bytes()
            files[f"/{filename}"] = data
            entries.append(
                {
                    "filename": filename,
                    "url": f"/{filename}",
                    "hashes": {"sha256": hashlib.sha256(data).hexdigest()},
                    "requires-python": ">=3.11",
                    "upload-time": f"2026-01-{day:02}T00:00:00Z",
                    "size": len(data),
                }
            )
        files[f"/simple/{name}/"] = json.dumps(
            {"meta": {"api-version": "1.1"}, "name": name, "files": entries}
        ).encode()

    class Index(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            data = files.get(self.path)
            self.send_response(200 if data else 404)
            self.send_header(
                "Content-Type",
                "application/vnd.pypi.simple.v1+json"
                if self.path.startswith("/simple/")
                else "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(data or b"")))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data or b"")

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Index)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname="fixture"\nversion="0"\nrequires-python=">=3.11"\n'
        'dependencies=["direct>=1,<2"]\n[tool.uv]\nrequired-version="==0.13.5"\n'
        "[[tool.uv.index]]\ndefault=true\n"
        f'url="http://127.0.0.1:{server.server_port}/simple"\n'
    )
    try:
        run_uv(project, "--exclude-newer", "2026-01-02T00:00:00Z")
        assert versions(project) == {"direct": "1.0", "indirect": "1.0"}
        yield project
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_uv(project: Path, *args: str) -> None:
    subprocess.run(
        ["uv", "lock", "--python", sys.executable, "--no-cache", *args],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )


def versions(project: Path) -> dict[str, str]:
    lock = tomllib.loads((project / "uv.lock").read_text())
    return {
        package["name"]: package["version"]
        for package in lock["package"]
        if "registry" in package["source"]
    }


def test_weekly_refresh_advances_eligible_direct_and_indirect_versions(
    updater: ModuleType, project: Path
) -> None:
    updater.refresh(project, upgrade=True, now=NOW)
    assert versions(project) == {"direct": "1.1", "indirect": "1.1"}
    run_uv(project, "--check")
    subprocess.run(
        ["uv", "sync", "--locked", "--no-install-project", "--python", sys.executable],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )


def test_security_resolution_accepts_fresh_direct_and_indirect_fixes(
    updater: ModuleType, project: Path
) -> None:
    manifest = project / "pyproject.toml"
    manifest.write_text(
        manifest.read_text().replace("direct>=1,<2", "direct>=1.2,<1.3")
    )
    updater.refresh(project, security=True, now=NOW)
    assert versions(project) == {"direct": "1.2", "indirect": "1.3"}
    run_uv(project, "--check")
    updater.refresh(project, upgrade=True, now=NOW)
    assert versions(project) == {"direct": "1.2", "indirect": "1.3"}
    run_uv(project, "--check")


@pytest.mark.parametrize("now", [NOW, datetime(2026, 1, 26, tzinfo=UTC)])
def test_routine_refresh_preserves_only_already_locked_fresh_fixes(
    updater: ModuleType, project: Path, now: datetime
) -> None:
    run_uv(
        project,
        "--upgrade-package",
        "direct==1.2",
        "--upgrade-package",
        "indirect==1.2",
    )
    assert versions(project) == {"direct": "1.2", "indirect": "1.2"}
    updater.refresh(project, upgrade=True, now=now)
    assert versions(project) == {"direct": "1.2", "indirect": "1.2"}
    run_uv(project, "--check")
    previous = (project / "uv.lock").read_bytes()
    updater.refresh(project, upgrade=True, now=now)
    assert (project / "uv.lock").read_bytes() == previous


def test_helper_raises_floor_without_adopting_fresh_transitives(
    updater: ModuleType, project: Path
) -> None:
    manifest = project / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace("direct>=1,<2", "direct>=1.1,<2"))
    updater.refresh(project, now=NOW)
    assert versions(project) == {"direct": "1.1", "indirect": "1.0"}
    run_uv(project, "--check")


def test_routine_helper_rejects_a_floor_still_inside_cooldown(
    updater: ModuleType, project: Path
) -> None:
    manifest = project / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace("direct>=1,<2", "direct>=1.2,<2"))
    previous = (project / "uv.lock").read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        updater.refresh(project, now=NOW)
    assert (project / "uv.lock").read_bytes() == previous


def test_dev_ranges_do_not_admit_unreviewed_major_versions() -> None:
    project = tomllib.loads((ROOT / "api/pyproject.toml").read_text())
    dev = project["dependency-groups"]["dev"]
    for name, cap in (("mypy", "3"), ("pytest", "10"), ("import-linter", "3")):
        requirement = next(item for item in dev if item.startswith(f"{name}>="))
        assert f",<{cap}" in requirement


@pytest.mark.parametrize(
    ("body", "message", "security"),
    [
        ("Bumps direct.", "Fix GHSA-abcd-1234-5678", True),
        ("Fixes GHSA-abcd-1234-5678", "Bump direct", True),
        ("Bumps direct.\n<details>\n## Security\nGHSA-abcd-1234-5678", "Bump", False),
        ("Bumps direct.", "Bump direct", False),
    ],
)
def test_helper_uses_the_existing_security_classification(
    updater: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    message: str,
    security: bool,
) -> None:
    responses = iter(
        [
            json.dumps({"user": {"login": "dependabot[bot]"}, "body": body}),
            json.dumps([[{"commit": {"message": message}}]]),
        ]
    )
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_kw: next(responses))
    assert updater.dependabot_security("owner/repo", 12) is security


def test_helper_rejects_a_non_dependabot_security_claim(
    updater: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_a, **_kw: json.dumps(
            {"user": {"login": "human"}, "body": "GHSA-abcd-1234-5678"}
        ),
    )
    with pytest.raises(ValueError, match="Dependabot-authored"):
        updater.dependabot_security("owner/repo", 12)


def test_automation_wires_floor_updates_and_trusted_resolver_code() -> None:
    config = (ROOT / ".github/dependabot.yml").read_text()
    python = config.split("# --- Go worker ---")[0]
    assert "package-ecosystem: pip" in python
    assert "versioning-strategy: increase" in python
    helper = (ROOT / ".github/workflows/dependabot-uv-lock.yml").read_text()
    assert "ref: ${{ github.event.pull_request.base.sha }}" in helper
    assert (
        ".automation/scripts/refresh_python_lock.py --project api --dependabot-pr"
        in helper
    )
    weekly = (ROOT / ".github/workflows/python-lock-refresh.yml").read_text()
    assert 'cron: "0 4 * * 1"' in weekly
    assert "workflow_dispatch:" in weekly
    assert "scripts/refresh_python_lock.py --upgrade" in weekly
    assert "--force-with-lease=" in weekly
    assert "gh pr list --head" in weekly
    assert "--label dependencies" in weekly
    assert "gh pr merge" not in weekly
