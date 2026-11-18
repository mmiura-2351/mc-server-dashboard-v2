"""The api workflow runs whenever a Web UI file an api test reads changes.

Some api tests pin a contract the Web UI mirrors by reading the Web UI's own
copy of it. The api workflow is path-filtered, so a change to such a file alone
would otherwise never run the test that pins it (issue #3929).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _webui_paths(source: str) -> set[str]:
    # A path can be spelled as literals joined by `/`: join them first.
    joined = re.sub(r'"\s*/\s*"', "/", source)
    return set(re.findall(r'"(webui/[^"]+)"', joined))


def _filter_paths(trigger: str) -> set[str]:
    workflow = (ROOT / ".github/workflows/api.yml").read_text()
    block = re.search(rf"^  {trigger}:\n((?:    .*\n)+)", workflow, re.MULTILINE)
    assert block is not None, trigger
    return set(re.findall(r'^      - "([^"]+)"$', block[1], re.MULTILINE))


def test_the_api_workflow_runs_on_every_webui_file_an_api_test_reads() -> None:
    read = set[str]().union(
        *(_webui_paths(test.read_text()) for test in (ROOT / "api/tests").rglob("*.py"))
    )

    assert read, "the scan found no api test reading a Web UI file"
    for trigger in ("pull_request", "push"):
        assert sorted(read - _filter_paths(trigger)) == [], trigger
