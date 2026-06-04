#!/usr/bin/env python3
"""Convention checks for docs/ (see docs/README.md Conventions).

Runs three gates over the Markdown under docs/ and fails loudly with
``file:line`` for every violation:

1. Relative Markdown links resolve to an existing file. Only relative links are
   checked (``http(s):``, ``mailto:`` and pure ``#anchor`` links are skipped);
   any ``#fragment`` or ``?query`` on a path is dropped before the file is
   resolved. Links inside backtick code spans are ignored, so illustrative link
   examples (the docs/README.md Conventions section) need no exemption.
2. No section-mark glyph (the U+00A7 character) anywhere in docs/.
3. No standalone ``v1`` versioning term. The convention forbids "v1" for the new
   system; the proto package label ``mcsd.controlplane.v1`` and similar code
   spans are legitimate, so the check ignores anything inside backtick code
   spans and exempts the quoted form ``"v1"`` that the conventions themselves
   use to *name* the forbidden term.

Pure standard library; runs under any Python 3.8+ (the api/ venv or a system
python). Exit status is non-zero when any check fails.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# The section-mark glyph the conventions forbid (see docs/README.md).
SECTION_MARK = "§"

# A backtick code span: `...`. Stripped before the prose-only v1 check so that
# code labels like `mcsd.controlplane.v1` never trip it.
CODE_SPAN = re.compile(r"`[^`]*`")

# A standalone "v1" token: not part of a longer identifier (e.g. not the tail of
# ".v1" or "v12") and not glued to surrounding word characters.
V1_TOKEN = re.compile(r"(?<![.\w])v1(?![\w])")

# The quoted form the conventions use to *name* the forbidden term, e.g.
# `Never write "v1"`. Exempt so the rule can describe itself.
V1_QUOTED = '"v1"'

# A Markdown inline link target: [text](target).
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def is_relative_link(target: str) -> bool:
    """True for links that name a file path on disk (not a URL or bare anchor)."""
    if target.startswith("#"):
        return False
    if "://" in target:
        return False
    if target.startswith("mailto:"):
        return False
    return True


def check_links(path: Path, lines: list[str], docs_root: Path) -> list[str]:
    errors: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        # Strip code spans so illustrative link examples written as code spans
        # (e.g. the docs/README.md Conventions section) are not treated as real
        # links to resolve.
        line = CODE_SPAN.sub("", line)
        for match in LINK.finditer(line):
            target = match.group(1).strip()
            if not is_relative_link(target):
                continue
            # Drop a #fragment / ?query suffix; resolve the path part only.
            file_part = re.split(r"[#?]", target, maxsplit=1)[0]
            if not file_part:
                continue
            resolved = (path.parent / file_part).resolve()
            if not resolved.exists():
                rel = path.relative_to(docs_root.parent)
                errors.append(
                    f"{rel}:{lineno}: relative link does not resolve: {target}"
                )
    return errors


def check_section_mark(path: Path, lines: list[str], docs_root: Path) -> list[str]:
    errors: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        if SECTION_MARK in line:
            rel = path.relative_to(docs_root.parent)
            errors.append(
                f"{rel}:{lineno}: section-mark glyph ({SECTION_MARK}) is "
                "forbidden; write 'Section N.N'"
            )
    return errors


def check_v1(path: Path, lines: list[str], docs_root: Path) -> list[str]:
    errors: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        # Strip code spans so code labels like `mcsd.controlplane.v1` are exempt.
        prose = CODE_SPAN.sub("", line)
        # Exempt the quoted form used to define the rule itself.
        prose = prose.replace(V1_QUOTED, "")
        if V1_TOKEN.search(prose):
            rel = path.relative_to(docs_root.parent)
            errors.append(
                f"{rel}:{lineno}: standalone 'v1' is forbidden as a versioning "
                "term; use 'legacy' or 'v2' (see docs/README.md Conventions)"
            )
    return errors


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    docs_root = repo_root / "docs"
    if not docs_root.is_dir():
        print(f"docs/ not found at {docs_root}", file=sys.stderr)
        return 2

    errors: list[str] = []
    for path in sorted(docs_root.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        errors.extend(check_links(path, lines, docs_root))
        errors.extend(check_section_mark(path, lines, docs_root))
        errors.extend(check_v1(path, lines, docs_root))

    if errors:
        print("docs-check found violations:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    print("docs-check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
