#!/usr/bin/env python3
"""Convention checks for docs/ (see docs/README.md Conventions).

Runs three gates over the Markdown under docs/ and fails loudly with
``file:line`` for every violation:

1. Relative Markdown links resolve to an existing file. Only relative links are
   checked (``http(s):``, ``mailto:`` and pure ``#anchor`` links are skipped);
   any ``#fragment`` or ``?query`` on a path is dropped before the file is
   resolved, and an optional link title (``[x](path "title")``) is stripped so
   the path part alone is resolved. Links inside backtick code spans are
   ignored, so illustrative link examples (the docs/README.md Conventions
   section) need no exemption.
2. No section-mark glyph (the U+00A7 character) anywhere in docs/.
3. No standalone ``v1`` versioning term. The convention forbids "v1" for the new
   system; the proto package label ``mcsd.controlplane.v1`` and similar code
   spans are legitimate, so the check ignores anything inside backtick code
   spans and exempts the quoted form the conventions use to *name* the forbidden
   term (see V1_NAMING below).

All three gates first blank out fenced code blocks (``` fences) and backtick
code spans (single ``` `…` ``` and double ``` ``…`` ```) so illustrative code --
proto labels, command snippets, example links -- never trips a prose rule.

Pure standard library; runs under any Python 3.8+ (the api/ venv or a system
python). Exit status is non-zero when any check fails.

Run ``scripts/check_docs.py --self-test`` to exercise the checks against
in-memory fixtures (the helpers, not the docs tree). The self-test has no
dependencies and is invoked by the docs CI workflow alongside the real run.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# The section-mark glyph the conventions forbid (see docs/README.md).
SECTION_MARK = "§"

# Backtick code spans. Double-backtick spans are matched first so a ``…`` span
# that contains a single backtick is stripped whole (a lone-backtick pass would
# mis-split it). Stripped before the prose-only checks so code labels like
# `mcsd.controlplane.v1` never trip them.
CODE_SPAN = re.compile(r"``[^`]*``|`[^`]*`")

# A fenced code block delimiter: a line whose first non-space content is ``` or
# ~~~ (optionally with an info string). Everything between an opening and closing
# fence is non-prose and is blanked before the checks run.
FENCE = re.compile(r"^\s*(```+|~~~+)")

# A standalone "v1" token: not part of a longer identifier (e.g. not the tail of
# ".v1" or "v12") and not glued to surrounding word characters.
V1_TOKEN = re.compile(r"(?<![.\w])v1(?![\w])")

# The conventions name the forbidden term with a "Never write/called \"v1\""
# construct (docs/README.md, docs/REQUIREMENTS.md). Exempt only that self-naming
# form so the rule can describe itself; prose such as `the "v1" system` -- where
# "v1" is *used*, not named -- is still caught.
V1_NAMING = re.compile(r'\bNever (?:write|called) "v1"')

# A Markdown inline link target: [text](target). The target may carry an
# optional title: [text](path "title").
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def strip_code(line: str) -> str:
    """Blank out backtick code spans so code never trips a prose rule."""
    return CODE_SPAN.sub("", line)


def strip_fences(lines: list[str]) -> list[str]:
    """Replace lines inside fenced code blocks with empty strings.

    Line numbering is preserved (blanked, not dropped) so reported ``file:line``
    positions stay accurate.
    """
    out: list[str] = []
    in_fence = False
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return out


def is_relative_link(target: str) -> bool:
    """True for links that name a file path on disk (not a URL or bare anchor)."""
    if target.startswith("#"):
        return False
    if "://" in target:
        return False
    if target.startswith("mailto:"):
        return False
    return True


def link_path(target: str) -> str:
    """The on-disk path part of a link target.

    Drops an optional title (``path "title"`` or ``path 'title'``) and any
    ``#fragment`` / ``?query`` suffix, leaving the path to resolve.
    """
    # A title is whitespace-separated and quoted; take everything before it.
    target = re.split(r'\s+["\']', target, maxsplit=1)[0]
    return re.split(r"[#?]", target, maxsplit=1)[0]


def check_links(path_label: str, lines: list[str], link_root: Path) -> list[str]:
    errors: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        # Strip code spans so illustrative link examples written as code spans
        # (e.g. the docs/README.md Conventions section) are not treated as real
        # links to resolve.
        line = strip_code(line)
        for match in LINK.finditer(line):
            target = match.group(1).strip()
            if not is_relative_link(target):
                continue
            file_part = link_path(target)
            if not file_part:
                continue
            resolved = (link_root / file_part).resolve()
            if not resolved.exists():
                errors.append(
                    f"{path_label}:{lineno}: relative link does not resolve: {target}"
                )
    return errors


def check_section_mark(path_label: str, lines: list[str]) -> list[str]:
    errors: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        if SECTION_MARK in strip_code(line):
            errors.append(
                f"{path_label}:{lineno}: section-mark glyph ({SECTION_MARK}) is "
                "forbidden; write 'Section N.N'"
            )
    return errors


def check_v1(path_label: str, lines: list[str]) -> list[str]:
    errors: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        # Strip code spans so code labels like `mcsd.controlplane.v1` are exempt.
        prose = strip_code(line)
        # Drop the conventions' own self-naming form so the rule can describe
        # itself, while leaving any *use* of "v1" in prose to be caught.
        prose = V1_NAMING.sub("", prose)
        if V1_TOKEN.search(prose):
            errors.append(
                f"{path_label}:{lineno}: standalone 'v1' is forbidden as a "
                "versioning term; use 'legacy' or 'v2' (see docs/README.md "
                "Conventions)"
            )
    return errors


def check_file(path: Path, docs_root: Path) -> list[str]:
    label = str(path.relative_to(docs_root.parent))
    lines = strip_fences(path.read_text(encoding="utf-8").splitlines())
    errors = check_links(label, lines, path.parent)
    errors.extend(check_section_mark(label, lines))
    errors.extend(check_v1(label, lines))
    return errors


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    docs_root = repo_root / "docs"
    if not docs_root.is_dir():
        print(f"docs/ not found at {docs_root}", file=sys.stderr)
        return 2

    errors: list[str] = []
    for path in sorted(docs_root.rglob("*.md")):
        errors.extend(check_file(path, docs_root))

    if errors:
        print("docs-check found violations:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    print("docs-check: OK")
    return 0


def _self_test() -> int:
    """Exercise the gates against in-memory fixtures (no docs-tree dependency)."""
    root = Path(__file__).resolve().parent.parent
    failures: list[str] = []

    def expect(name: str, got: list[str], should_flag: bool) -> None:
        flagged = bool(got)
        if flagged != should_flag:
            failures.append(
                f"{name}: expected {'a violation' if should_flag else 'no violation'}, "
                f"got {got!r}"
            )

    # Section-mark glyph: caught in prose, exempt inside code spans / fences.
    expect("glyph prose", check_section_mark("f", ["See Section 5 §5."]), True)
    expect("glyph code span", check_section_mark("f", ["A label `a § b`."]), False)
    expect("glyph fence", check_section_mark("f", strip_fences(["```", "§", "```"])), False)

    # v1: code labels and the conventions' self-naming form are exempt; prose use
    # and a double-backtick span around code stay correct.
    expect("v1 code span", check_v1("f", ["The `mcsd.controlplane.v1` package."]), False)
    expect("v1 double span", check_v1("f", ["The ``foo `v1` bar`` snippet."]), False)
    expect("v1 naming readme", check_v1("f", ['Never write "v1" for the new system.']), False)
    expect("v1 naming reqs", check_v1("f", ['Never called "v1" here, to avoid ambiguity.']), False)
    expect("v1 prose quoted", check_v1("f", ['Migrate the "v1" system away.']), True)
    expect("v1 prose bare", check_v1("f", ["Targeting v1 of the agent."]), True)
    expect("v1 fence", check_v1("f", strip_fences(["```", "version: v1", "```"])), False)

    # Links: titles and fragments are stripped before resolution; an existing
    # path resolves, a missing one flags; code-span examples are exempt.
    expect(
        "link title ok",
        check_links("f", ['See [x](README.md "the readme").'], root / "docs"),
        False,
    )
    expect(
        "link missing",
        check_links("f", ["See [x](does-not-exist.md)."], root / "docs"),
        True,
    )
    expect(
        "link code span",
        check_links("f", ["Example: `[x](nope.md)`."], root / "docs"),
        False,
    )

    if failures:
        print("check_docs --self-test FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("check_docs --self-test: OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit(main())
