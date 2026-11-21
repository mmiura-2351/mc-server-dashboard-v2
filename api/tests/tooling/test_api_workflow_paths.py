"""The api workflow runs whenever a repo file an api test reads changes.

Some api tests read files outside ``api/``. A file can be the Web UI's copy of
a contract the test pins, a deployment file, or a script under test. The api
workflow is path-filtered, so a change to such a file alone would otherwise
never run the test that reads it (issue #3929).

The scan treats every string literal in ``api/tests`` as a path from the repo
root. A literal that names a file git tracks counts as a read; an untracked
file never does, so every checkout gets the same answer. A literal that names a
tracked file without reading it goes in ``_NOT_READS``. Known limit: a path
spelled with ``joinpath``, ``os.path.join``, a variable or an f-string, rather
than as literals joined by ``/``, is not seen, and neither is a directory.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

# Literals in api/tests that name a tracked file but are not reads of it, each
# with the test it is in.
_NOT_READS: frozenset[str] = frozenset()


def _tracked() -> set[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return set(listing.split("\0")) - {""}


def _repo_files(source: str, tracked: set[str]) -> set[str]:
    # A path can be spelled as literals joined by `/`: join them first.
    joined = re.sub(r'"\s*/\s*"', "/", source)
    literals = re.findall(r'"([^"\n]+)"', joined)
    return {os.path.normpath(literal) for literal in literals} & tracked


def _filter_paths(trigger: str) -> list[str]:
    workflow = (ROOT / ".github/workflows/api.yml").read_text()
    block = re.search(rf"^  {trigger}:\n((?:    .*\n)+)", workflow, re.MULTILINE)
    assert block is not None, trigger
    return re.findall(r'^      - "([^"]+)"$', block[1], re.MULTILINE)


_WILDCARDS = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*"}


def _listed(path: str, patterns: list[str]) -> bool:
    # GitHub's filter globs: `*` stays within one path segment, `**` crosses
    # `/`, and `**/` also matches no directory. The last pattern that matches
    # decides, and a `!` one excludes. GitHub's `?`, `+` and `[]` match
    # themselves here; no filter entry uses them.
    listed = False
    for pattern in patterns:
        glob = pattern.removeprefix("!")
        tokens = re.split(r"(\*\*/|\*\*|\*)", glob)
        regex = "".join(_WILDCARDS.get(token, re.escape(token)) for token in tokens)
        if re.fullmatch(regex, path):
            listed = glob == pattern
    return listed


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
    tracked = _tracked()
    tests = (ROOT / "api/tests").rglob("*.py")
    read = set[str]().union(*(_repo_files(t.read_text(), tracked) for t in tests))
    read -= _NOT_READS

    assert read - {".github/workflows/api.yml"}, "the scan found no file it guards"
    for trigger in ("pull_request", "push"):
        patterns = _filter_paths(trigger)
        unlisted = sorted(path for path in read if not _listed(path, patterns))
        assert unlisted == [], (
            f"api tests read {unlisted}, which the `{trigger}` paths of"
            " .github/workflows/api.yml do not list: add each to both triggers"
        )
