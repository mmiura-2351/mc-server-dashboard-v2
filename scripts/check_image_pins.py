#!/usr/bin/env python3
"""Guard: the CI image pins follow the images ``compose.yaml`` deploys (#2905).

CI runs PostgreSQL and SeaweedFS from digest pins that no Dependabot ecosystem
watches, so they are re-pinned by hand in the Dependabot PR that moves the
matching ``compose.yaml`` image (docs/dev/DEPENDENCIES.md Section 6). A
documented-only step was missed once already: ``api.yml`` kept testing
SeaweedFS 4.41 against a 4.42 deployment and nothing reported it (#2904).

The CI side pins by digest and ``compose.yaml`` by tag, and resolving a digest
needs the network, so this compares the tag recorded in the comment above each
digest instead -- which also keeps that comment truthful. For every pin site in
``SITES``:

1. The site has at least one ``<image>@sha256:<digest>`` pin.
2. The same block (no blank line in between) has a ``<image>:<tag>`` mention
   above the pin -- its pin comment -- and the nearest such mention names
   exactly the tag ``compose.yaml``'s ``image: <image>:<tag>`` line deploys.
3. Every pin of one image, across all its sites, carries the same digest: they
   all follow the one tag, so a digest that differs is a re-pin that stopped
   halfway with the comments already updated.

The digest itself is not verified against the tag; that is the re-pin
procedure's job (DEPENDENCIES.md Section 6), not a gate's.

``SITES`` mirrors the enumeration in DEPENDENCIES.md Section 6 and is updated
with it: a site that no longer pins its image fails check 1 rather than
silently dropping out.

Pure standard library, no YAML parser; runs under any Python 3.8+. Offline and
sub-second. Exit status is non-zero when any check fails.

Run ``scripts/check_image_pins.py --self-test`` to exercise the checks against
fixtures (not the real tree).
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

COMPOSE = "compose.yaml"

# image -> (the DEPENDENCIES.md Section 6 paragraph with its re-pin procedure,
#           the files that pin it by digest).
SITES: dict[str, tuple[str, tuple[str, ...]]] = {
    "postgres": (
        "PostgreSQL: CI runs the minor the deployment runs",
        (
            ".github/workflows/api.yml",
            ".github/workflows/e2e.yml",
            ".github/workflows/webui-e2e.yml",
            "scripts/run_webui_e2e.sh",
        ),
    ),
    "chrislusf/seaweedfs": (
        "SeaweedFS: the CI pin is the tag's image-index digest",
        (".github/workflows/api.yml",),
    ),
}

# Not preceded by a character that would make the match part of a longer image
# name (``library/postgres``, ``my-postgres``).
_BOUNDARY = r"(?<![\w./-])"


def _tag_pattern(image: str) -> re.Pattern[str]:
    """``<image>:<tag>`` as a pin comment writes it; group 1 is the tag.

    The tag starts with a digit, so a YAML key named after the image
    (``postgres:`` under ``services:``) is not a mention, and it ends on a word
    character, so a sentence-final period is not part of it.
    """
    return re.compile(_BOUNDARY + re.escape(image) + r":(\d(?:[\w.-]*\w)?)")


def _digest_pattern(image: str) -> re.Pattern[str]:
    return re.compile(_BOUNDARY + re.escape(image) + r"@(sha256:[0-9a-f]{64})")


def _compose_pattern(image: str) -> re.Pattern[str]:
    """An ``image: <image>:<tag>`` line; a comment line never matches."""
    return re.compile(
        r"^\s*image:\s*[\"']?" + re.escape(image) + r":([\w][\w.-]*)", re.MULTILINE
    )


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def check_image(root: Path, image: str) -> list[str]:
    """Violation messages for ``image`` (empty when its pins follow compose).

    Messages carry ``file:line`` relative to ``root``, in site order, so one run
    lists every pin a re-pin has to touch.
    """
    compose_text = (root / COMPOSE).read_text(encoding="utf-8")
    deployed = list(_compose_pattern(image).finditer(compose_text))
    if len(deployed) != 1:
        return [
            f"{COMPOSE}: expected exactly one `image: {image}:<tag>` line, "
            f"found {len(deployed)}"
        ]
    tag = deployed[0].group(1)
    compose_ref = f"{COMPOSE}:{_line_of(compose_text, deployed[0].start(1))}"

    tag_pattern = _tag_pattern(image)
    errors: list[str] = []
    digests: dict[str, list[str]] = {}
    for rel in SITES[image][1]:
        path = root / rel
        if not path.is_file():
            errors.append(f"{rel}: not found")
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        pins = [
            (number, match.group(1))
            for number, line in enumerate(lines, start=1)
            for match in _digest_pattern(image).finditer(line)
        ]
        if not pins:
            errors.append(
                f"{rel}: no `{image}@sha256:<digest>` pin found -- if this file "
                f"no longer pins {image}, update SITES in "
                "scripts/check_image_pins.py and docs/dev/DEPENDENCIES.md Section 6"
            )
        for number, digest in pins:
            digests.setdefault(digest, []).append(f"{rel}:{number}")
            comment = None
            # Walk up the pin's own block: a blank line ends it.
            for above in range(number - 1, 0, -1):
                if not lines[above - 1].strip():
                    break
                mention = tag_pattern.search(lines[above - 1])
                if mention:
                    comment = (above, mention.group(1))
                    break
            if comment is None:
                errors.append(
                    f"{rel}:{number}: no `{image}:<tag>` comment above this pin in "
                    f"its block -- record the {COMPOSE} tag it pins, "
                    f"`{image}:{tag}`, in the comment above it"
                )
            elif comment[1] != tag:
                errors.append(
                    f"{rel}:{comment[0]}: pin comment says {image}:{comment[1]}, "
                    f"but {compose_ref} deploys {image}:{tag} -- re-pin the "
                    f"digest on line {number} to {image}:{tag} and update this "
                    "comment"
                )

    if len(digests) > 1:
        groups = "; ".join(
            f"{digest[:19]}... at {', '.join(where)}"
            for digest, where in digests.items()
        )
        errors.append(
            f"the {image} pins carry different digests, but they all follow "
            f"{compose_ref}'s one tag: {groups}"
        )
    return errors


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    failed = False
    for image, (procedure, _sites) in SITES.items():
        errors = check_image(repo_root, image)
        if not errors:
            continue
        failed = True
        print(
            f"check-image-pins: the {image} CI pins do not follow {COMPOSE} (#2905):",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        print(
            "  Re-pin the digest and the comment above it in one change, in the "
            f"PR that moves {COMPOSE}; procedure: docs/dev/DEPENDENCIES.md "
            f'Section 6, "{procedure}".',
            file=sys.stderr,
        )
    if failed:
        return 1
    counts = ", ".join(f"{image} x{len(sites)}" for image, (_, sites) in SITES.items())
    print(f"check-image-pins: OK (CI sites follow {COMPOSE}: {counts})")
    return 0


def _self_test() -> int:
    """Exercise the checks against fixtures (no real tree dependency)."""
    failures: list[str] = []

    pg_digest = "sha256:" + "4e" * 32
    sw_digest = "sha256:" + "fc" * 32

    compose = (
        "services:\n"
        "  db:\n"
        "    image: postgres:18.6\n"
        "  seaweedfs:\n"
        "    # Verified against chrislusf/seaweedfs:4.41 (a comment, not the pin).\n"
        "    image: chrislusf/seaweedfs:4.45\n"
    )

    def service_pin(tag: str = "18.6", digest: str = pg_digest) -> str:
        """The ``services:`` shape api.yml / e2e.yml / webui-e2e.yml use."""
        return (
            "jobs:\n"
            "  check:\n"
            "    services:\n"
            "      postgres:\n"
            f"        # postgres:{tag} (Debian; pushed 2026-08-26, outside the\n"
            "        # cooldown) -- the tag compose.yaml pins.\n"
            f"        image: postgres@{digest}\n"
            "        env:\n"
            "          POSTGRES_USER: mcsd\n"
        )

    def seaweedfs_step(tag: str = "4.45", digest: str = sw_digest) -> str:
        """api.yml's live-s3 shape: the comment sits lines above the digest."""
        return (
            "      - name: Start SeaweedFS\n"
            "        run: |\n"
            "          mkdir -p seaweedfs\n"
            "\n"
            f"          # chrislusf/seaweedfs:{tag} (pushed 2026-09-01, outside the\n"
            "          # cooldown) -- the version compose.yaml pins. It sat on 4.39\n"
            "          # for two compose bumps (issue #2614).\n"
            "          docker run -d --name seaweedfs \\\n"
            "            --entrypoint weed \\\n"
            f"            chrislusf/seaweedfs@{digest} \\\n"
            "            server -dir=/data -s3\n"
        )

    def shell_pin(tag: str = "18.6", digest: str = pg_digest) -> str:
        """run_webui_e2e.sh's shape."""
        return (
            'PG_PORT="5432"\n'
            f"# postgres:{tag} (Debian), the api.yml-vetted digest.\n"
            f'PG_IMAGE="postgres@{digest}"\n'
        )

    def tree(**override: str) -> dict[str, str]:
        """A clean fixture tree; ``override`` maps a file key to new content."""
        files = {
            "compose": compose,
            "api": service_pin() + seaweedfs_step(),
            "e2e": service_pin(),
            "webui_e2e": service_pin(),
            "script": shell_pin(),
        }
        files.update(override)
        return {
            COMPOSE: files["compose"],
            ".github/workflows/api.yml": files["api"],
            ".github/workflows/e2e.yml": files["e2e"],
            ".github/workflows/webui-e2e.yml": files["webui_e2e"],
            "scripts/run_webui_e2e.sh": files["script"],
        }

    def run(files: dict[str, str], image: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
            return check_image(root, image)

    def expect(
        name: str, files: dict[str, str], image: str, want: list[list[str]]
    ) -> None:
        """One violation per entry of ``want``, containing each of its parts."""
        got = run(files, image)
        if len(got) != len(want) or not all(
            all(part in err for part in parts) for err, parts in zip(got, want)
        ):
            failures.append(f"{name}: expected violations {want!r}, got {got!r}")

    # A tree whose pins all follow compose.yaml is clean for both images.
    expect("clean postgres", tree(), "postgres", [])
    expect("clean seaweedfs", tree(), "chrislusf/seaweedfs", [])

    # The #3057 shape: Dependabot moved compose.yaml and nothing else. The
    # message names the pin comment and the digest line, so the person holding
    # the Dependabot PR knows exactly what to re-pin.
    bumped = tree(compose=compose.replace("seaweedfs:4.45", "seaweedfs:4.47"))
    expect(
        "compose bumped, CI untouched",
        bumped,
        "chrislusf/seaweedfs",
        [
            [
                ".github/workflows/api.yml:14:",
                "chrislusf/seaweedfs:4.45",
                "compose.yaml:6",
                "chrislusf/seaweedfs:4.47",
                "digest on line 19",
            ]
        ],
    )
    # ... and it leaves postgres, which it did not touch, clean.
    expect("compose bumped, other image", bumped, "postgres", [])

    # A postgres bump names every one of the four stale sites, not just the
    # first, so one run lists the whole re-pin.
    pg_bumped = tree(compose=compose.replace("postgres:18.6", "postgres:18.7"))
    expect(
        "postgres bumped, CI untouched",
        pg_bumped,
        "postgres",
        [
            [".github/workflows/api.yml:5:", "postgres:18.6", "postgres:18.7"],
            [".github/workflows/e2e.yml:5:"],
            [".github/workflows/webui-e2e.yml:5:"],
            ["scripts/run_webui_e2e.sh:2:"],
        ],
    )

    # The whole re-pin landed except one comment (digest re-pinned everywhere).
    new_digest = "sha256:" + "18" * 32
    expect(
        "one stale comment",
        tree(
            compose=compose.replace("postgres:18.6", "postgres:18.7"),
            api=service_pin("18.7", new_digest) + seaweedfs_step(),
            e2e=service_pin("18.7", new_digest),
            webui_e2e=service_pin("18.6", new_digest),
            script=shell_pin("18.7", new_digest),
        ),
        "postgres",
        [[".github/workflows/webui-e2e.yml:5:", "postgres:18.6", "postgres:18.7"]],
    )

    # Every comment updated, one digest left behind: the comments agree with
    # compose.yaml, so only the digest comparison can see it.
    expect(
        "one stale digest",
        tree(
            compose=compose.replace("postgres:18.6", "postgres:18.7"),
            api=service_pin("18.7", new_digest) + seaweedfs_step(),
            e2e=service_pin("18.7", new_digest),
            webui_e2e=service_pin("18.7", new_digest),
            script=shell_pin("18.7"),
        ),
        "postgres",
        [["differ", "scripts/run_webui_e2e.sh:3", ".github/workflows/api.yml:7"]],
    )

    # A pin with no comment in its block: a matching mention in an earlier
    # block is not its comment.
    uncommented = '# postgres:18.6 (Debian)\n\nPG_IMAGE="postgres@' + pg_digest + '"\n'
    expect(
        "no pin comment",
        tree(script=uncommented),
        "postgres",
        [["scripts/run_webui_e2e.sh:3:", "no `postgres:<tag>` comment"]],
    )

    # The nearest mention is the pin comment; an older one above it in the same
    # block does not vouch for the pin.
    expect(
        "nearest mention wins",
        tree(script="# was postgres:18.6\n" + shell_pin("18.5")),
        "postgres",
        [["scripts/run_webui_e2e.sh:3:", "postgres:18.5"]],
    )

    # A sentence-final period is not part of the tag.
    expect(
        "trailing period",
        tree(
            script='# Pinned to postgres:18.6.\nPG_IMAGE="postgres@' + pg_digest + '"\n'
        ),
        "postgres",
        [],
    )

    # A site that stopped pinning its image fails instead of dropping out.
    expect(
        "site without a pin",
        tree(e2e="jobs: {}\n"),
        "postgres",
        [[".github/workflows/e2e.yml", "no `postgres@sha256:"]],
    )
    expect(
        "missing site",
        {k: v for k, v in tree().items() if k != "scripts/run_webui_e2e.sh"},
        "postgres",
        [["scripts/run_webui_e2e.sh", "not found"]],
    )

    # compose.yaml must deploy the image from exactly one `image:` line.
    expect(
        "compose without the image",
        tree(compose="services:\n  db:\n    image: mariadb:11\n"),
        "postgres",
        [[COMPOSE, "found 0"]],
    )
    expect(
        "compose with the image twice",
        tree(compose=compose + "  db2:\n    image: postgres:17.2\n"),
        "postgres",
        [[COMPOSE, "found 2"]],
    )

    if failures:
        print("check_image_pins --self-test FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("check_image_pins --self-test: OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit(main())
