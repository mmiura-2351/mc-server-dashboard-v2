#!/usr/bin/env python3
"""Keep API benchmark collection separate from the normal pytest suite (#3155).

The API pytest configuration disables the benchmark plugin by default, but that
does not prevent benchmark functions from being collected and run once as
ordinary tests.  This guard exercises both collection paths so a future change
cannot accidentally make ``api-test`` and ``bench-api`` collect the same tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_BENCHMARK_PREFIX = "tests/benchmarks/"


def _collect(api_root: Path, *args: str) -> list[str]:
    """Collect API tests and return collected nodeids."""
    result = subprocess.run(
        ["uv", "run", "pytest", "--collect-only", "-q", *args],
        cwd=api_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout, end="", file=sys.stderr)
        print(result.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"pytest collection failed with status {result.returncode}")
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    api_root = Path(__file__).resolve().parent.parent / "api"
    try:
        normal = _collect(api_root)
        benchmark = _collect(api_root, "tests/benchmarks", "--benchmark-enable")
    except RuntimeError as exc:
        print(f"api-benchmark-collection-check: {exc}", file=sys.stderr)
        return 1

    normal_benchmarks = [
        nodeid for nodeid in normal if nodeid.startswith(_BENCHMARK_PREFIX)
    ]
    if normal_benchmarks:
        print(
            "api-benchmark-collection-check: normal collection includes benchmarks:",
            file=sys.stderr,
        )
        for nodeid in normal_benchmarks:
            print(f"  {nodeid}", file=sys.stderr)
        return 1

    if not any(nodeid.startswith(_BENCHMARK_PREFIX) for nodeid in benchmark):
        print(
            "api-benchmark-collection-check: bench-api collection found no "
            "benchmark cases",
            file=sys.stderr,
        )
        return 1

    print("api-benchmark-collection-check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
