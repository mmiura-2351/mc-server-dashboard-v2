"""Server slug: generation and validation (RELAY.md Section 3, issue #955).

A slug is a deployment-wide unique DNS label assigned to a server. It is used as
the hostname prefix for the relay ingress path (``<slug>.<base_domain>``). This
module is the pure policy layer — no I/O — following the ports.py style.

**Format.** A slug is a valid lowercase DNS label:
``^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`` (1–63 characters, no leading/trailing
hyphen). In addition, a small reserved-word list blocks operational hostnames.

**Auto-generation.** The ``generate_slug`` function produces ``<word>-<word>-<NN>``
from a small embedded wordlist plus two zero-padded digits. The caller is
responsible for uniqueness retries — see :func:`generate_slug`.
"""

from __future__ import annotations

import random
import re

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidSlugError,
    SlugExhaustedError,
)

# DNS label regex: 1–63 chars, lowercase alphanumeric + hyphens, no
# leading/trailing hyphen.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# Reserved words that must not be used as slugs to keep operational hostnames
# available under the relay's base domain.
_RESERVED: frozenset[str] = frozenset(
    {
        "www",
        "api",
        "mail",
        "relay",
        "admin",
        "ns1",
        "ns2",
        "mc",
        "smtp",
        "pop",
        "imap",
        "ftp",
        "ssh",
        "vpn",
        "gateway",
    }
)

# Small embedded wordlist for auto-generation (adjective-like + animal-like).
# Two parts: ``<word>-<word>-<NN>`` yields 84 * 82 * 100 = 688 800 slots
# before exhaustion, which is far more than this system's scale (NFR-SCALE-1).
_WORDS_A: tuple[str, ...] = (
    "amber",
    "arctic",
    "azure",
    "bold",
    "bright",
    "calm",
    "cedar",
    "clear",
    "coral",
    "crisp",
    "cyan",
    "dark",
    "dawn",
    "deep",
    "dusk",
    "elder",
    "ember",
    "fern",
    "flame",
    "flint",
    "frost",
    "golden",
    "green",
    "grey",
    "indigo",
    "iron",
    "jade",
    "keen",
    "lake",
    "lark",
    "lemon",
    "light",
    "lime",
    "lunar",
    "maple",
    "marsh",
    "mist",
    "moss",
    "night",
    "north",
    "oak",
    "ocean",
    "olive",
    "opal",
    "pine",
    "plain",
    "polar",
    "rapid",
    "reef",
    "rose",
    "ruby",
    "sage",
    "sand",
    "sea",
    "sharp",
    "silent",
    "silver",
    "sky",
    "slate",
    "snow",
    "solar",
    "south",
    "stark",
    "star",
    "steel",
    "stone",
    "storm",
    "sunny",
    "swift",
    "teal",
    "terra",
    "tide",
    "timber",
    "true",
    "vale",
    "verdant",
    "violet",
    "vivid",
    "wave",
    "west",
    "wild",
    "wind",
    "winter",
    "wood",
)

_WORDS_B: tuple[str, ...] = (
    "ant",
    "asp",
    "bass",
    "bear",
    "bee",
    "bison",
    "boar",
    "buck",
    "bull",
    "carp",
    "cat",
    "clam",
    "cod",
    "condor",
    "crab",
    "crane",
    "crow",
    "cub",
    "dart",
    "deer",
    "doe",
    "dove",
    "duck",
    "eagle",
    "eel",
    "elk",
    "falcon",
    "finch",
    "fish",
    "flea",
    "fly",
    "fox",
    "frog",
    "gull",
    "hare",
    "hawk",
    "heron",
    "ibis",
    "jay",
    "kite",
    "lamb",
    "lark",
    "lion",
    "lynx",
    "mink",
    "mole",
    "moth",
    "mule",
    "newt",
    "owl",
    "perch",
    "pike",
    "plover",
    "pony",
    "puma",
    "quail",
    "ram",
    "raven",
    "ray",
    "robin",
    "rook",
    "seal",
    "shark",
    "shrew",
    "slug",
    "snipe",
    "sparrow",
    "stag",
    "starling",
    "stoat",
    "stork",
    "swift",
    "toad",
    "trout",
    "viper",
    "vole",
    "wasp",
    "weasel",
    "whale",
    "wren",
    "yak",
    "zebra",
)

# Maximum attempts to generate a unique slug before giving up. In practice
# collisions are extremely rare; this guards against degenerate uniqueness states
# (e.g. the database is almost full of slugs from this wordlist).
_MAX_ATTEMPTS = 20


def validate_slug(slug: str) -> None:
    """Validate a slug against the DNS-label regex and reserved-word list.

    Raises :class:`~mc_server_dashboard_api.servers.domain.errors.InvalidSlugError`
    when the slug does not match the DNS label format or is on the reserved list.
    Uppercase input is rejected with :class:`InvalidSlugError`; callers must not
    normalise — the API surfaces the error as a 422.
    """

    if not _SLUG_RE.fullmatch(slug):
        raise InvalidSlugError(slug)
    if slug in _RESERVED:
        raise InvalidSlugError(slug)


def generate_slug(*, taken: set[str]) -> str:
    """Return a fresh ``<word>-<word>-<NN>`` slug not in ``taken``.

    Draws from :data:`_WORDS_A`, :data:`_WORDS_B`, and a two-digit suffix
    (``00``–``99``) using :func:`random.choice` / :func:`random.randint`.
    Retries on collision (``slug in taken``) up to :data:`_MAX_ATTEMPTS` times,
    then raises
    :class:`~mc_server_dashboard_api.servers.domain.errors.SlugExhaustedError`.

    The caller passes the **deployment-wide** set of already-taken slugs (read
    from the repository inside the same transaction that will insert the new
    server row) so the uniqueness guarantee is transaction-local; the database
    ``UNIQUE`` constraint is the ultimate backstop.
    """

    for _ in range(_MAX_ATTEMPTS):
        candidate = (
            f"{random.choice(_WORDS_A)}"  # noqa: S311
            f"-{random.choice(_WORDS_B)}"  # noqa: S311
            f"-{random.randint(0, 99):02d}"  # noqa: S311
        )
        if candidate not in taken:
            return candidate
    raise SlugExhaustedError()
