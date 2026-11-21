"""The api workflow runs whenever a repo file an api test reads changes.

Some api tests read files outside ``api/``. A file can be the Web UI's copy of
a contract the test pins, a deployment file, or a script under test. The api
workflow is path-filtered, so a change to such a file alone would otherwise
never run the test that reads it (issue #3929).

The scan treats every string literal in ``api/tests`` as a path from the repo
root. A literal that names an existing repo file counts as a read. Known limit:
a path spelled with ``joinpath``, ``os.path.join``, a variable or an f-string,
rather than as literals joined by ``/``, is not seen, and neither is a
directory.
"""

from __future__ import annotations

import os
import re
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _repo_files(source: str) -> set[str]:
    # A path can be spelled as literals joined by `/`: join them first.
    joined = re.sub(r'"\s*/\s*"', "/", source)
    paths = {
        os.path.normpath(literal) for literal in re.findall(r'"([^"\n]+)"', joined)
    }
    return {
        path
        for path in paths
        if not os.path.isabs(path)
        and not path.startswith("..")
        and os.path.isfile(ROOT / path)
    }


def _filter_paths(trigger: str) -> list[str]:
    workflow = (ROOT / ".github/workflows/api.yml").read_text()
    block = re.search(rf"^  {trigger}:\n((?:    .*\n)+)", workflow, re.MULTILINE)
    assert block is not None, trigger
    return re.findall(r'^      - "([^"]+)"$', block[1], re.MULTILINE)


def _listed(path: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


@pytest.mark.parametrize(
    ("patterns", "path", "listed"),
    [
        # Names no tracked file, so the scan does not read this module's own cases.
        (["x.yml"], "x.yml", True),
        (["x.yml"], "x.yml.orig", False),
        # `*` stays within one path segment; `**` crosses `/`.
        (["web/*"], "web/x.ts", True),
        (["web/*"], "web/src/x.ts", False),
        (["web/**"], "web/src/x.ts", True),
        # `**/` matches no directory too.
        (["**/x.ts"], "x.ts", True),
        # The last pattern that matches decides, and a `!` one excludes.
        (["x.yml", "!x.yml"], "x.yml", False),
        (["web/**", "!web/*.json", "web/a.json"], "web/a.json", True),
        (["web/**", "!web/*.json"], "web/src/a.json", True),
    ],
)
def test_a_filter_lists_a_path_as_github_matches_it(
    patterns: list[str], path: str, listed: bool
) -> None:
    assert _listed(path, patterns) is listed


def test_the_api_workflow_runs_on_every_repo_file_an_api_test_reads() -> None:
    read = set[str]().union(
        *(_repo_files(test.read_text()) for test in (ROOT / "api/tests").rglob("*.py"))
    )

    assert read - {".github/workflows/api.yml"}, "the scan found no file it guards"
    for trigger in ("pull_request", "push"):
        patterns = _filter_paths(trigger)
        unlisted = sorted(path for path in read if not _listed(path, patterns))
        assert unlisted == [], trigger
