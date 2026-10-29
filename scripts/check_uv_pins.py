#!/usr/bin/env python3
"""Keep Docker, CI and bootstrap docs on api/pyproject.toml's exact uv version."""

from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import tomllib


def violations(root: pathlib.Path) -> list[str]:
    project = tomllib.loads((root / "api/pyproject.toml").read_text())
    required = project.get("tool", {}).get("uv", {}).get("required-version", "")
    if not re.fullmatch(r"==\d+\.\d+\.\d+", required):
        return ["api/pyproject.toml: tool.uv.required-version must pin ==X.Y.Z"]
    version = required.removeprefix("==")
    docker_pins = re.findall(
        r"(?m)^FROM ghcr\.io/astral-sh/uv:([^\s]+)",
        (root / "api/Dockerfile").read_text(),
    )
    errors = []
    if docker_pins != [version]:
        errors.append(f"api/Dockerfile: expected uv:{version}, found {docker_pins}")
    docs = "docs/dev/DEVELOPMENT.md"
    docs_pins = re.findall(
        r"\[uv\]\(https://docs\.astral\.sh/uv/\) ([\d.]+)",
        (root / docs).read_text(),
    )
    if docs_pins != [version]:
        errors.append(f"{docs}: expected uv {version}, found {docs_pins}")
    for workflow in sorted((root / ".github/workflows").glob("*.y*ml")):
        for step in re.split(r"(?m)^ +-[ \t]", workflow.read_text()):
            if "astral-sh/setup-uv@" not in step:
                continue
            pins = re.findall(r"(?m)^\s+version:\s*['\"]?([\d.]+)['\"]?\s*$", step)
            if pins != [version]:
                errors.append(
                    f"{workflow.relative_to(root)}: setup-uv must pin version: "
                    f'"{version}", found {pins}'
                )
    return errors


def _self_test() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        (root / "api").mkdir()
        (root / ".github/workflows").mkdir(parents=True)
        (root / "docs/dev").mkdir(parents=True)
        docs = root / "docs/dev/DEVELOPMENT.md"
        docs.write_text("| [uv](https://docs.astral.sh/uv/) 0.13.5 | Python |\n")
        (root / "api/pyproject.toml").write_text(
            '[tool.uv]\nrequired-version = "==0.13.5"\n'
        )
        (root / "api/Dockerfile").write_text(
            "FROM ghcr.io/astral-sh/uv:0.12.17 AS uv\n"
        )
        workflow = root / ".github/workflows/api.yml"
        workflow.write_text(
            "steps:\n  - uses: astral-sh/setup-uv@abc\n"
            '    with:\n      version: "0.13.5"\n'
        )
        errors = violations(root)
        assert any("api/Dockerfile" in error for error in errors), errors
        (root / "api/Dockerfile").write_text("FROM ghcr.io/astral-sh/uv:0.13.5 AS uv\n")
        assert violations(root) == []

        workflow.write_text("steps:\n  - uses: astral-sh/setup-uv@abc\n")
        assert any("api.yml" in error for error in violations(root))

        step = '  - uses: astral-sh/setup-uv@abc\n    with:\n      version: "0.13.5"\n'
        workflow.write_text("steps:\n" + step + step.replace("0.13.5", "0.12.17"))
        assert len(violations(root)) == 1, "check every setup-uv step"

        workflow.write_text("steps:\n" + step)
        new_workflow = root / ".github/workflows/new.yaml"
        new_workflow.write_text("steps:\n" + step.replace("0.13.5", "0.12.17"))
        assert any("new.yaml" in error for error in violations(root))
        new_workflow.unlink()

        docs.write_text("| [uv](https://docs.astral.sh/uv/) 0.12.17 | Python |\n")
        assert any("DEVELOPMENT.md" in error for error in violations(root))

        project = root / "api/pyproject.toml"
        project.write_text('[tool.uv]\nrequired-version = ">=0.13.5"\n')
        assert any("required-version" in error for error in violations(root))
        project.write_text("[tool.uv]\n")
        assert any("required-version" in error for error in violations(root))
    print("check_uv_pins self-test: ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    else:
        errors = violations(pathlib.Path(__file__).resolve().parent.parent)
        if errors:
            print("\n".join(errors), file=sys.stderr)
            sys.exit(1)
        print("uv pins: ok")
