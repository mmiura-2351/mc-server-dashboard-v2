"""Server slug: generation and validation (RELAY.md Section 3, issue #955).

A slug is a deployment-wide unique DNS label assigned to a server. It is used as
the hostname prefix for the relay ingress path (``<slug>.<base_domain>``). This
module is the pure policy layer — no I/O — following the ports.py style.

**Format.** A slug is a valid lowercase DNS label:
``^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`` (1–63 characters, no leading/trailing
hyphen). In addition, a small reserved-word list blocks operational hostnames.

**Auto-generation.** The ``generate_slug`` function produces a 6-character
lowercase-alphanumeric string (``[a-z0-9]{6}``) using the stdlib ``secrets``
module. The caller is responsible for uniqueness retries — see :func:`generate_slug`.
"""

from __future__ import annotations

import re
import secrets
import string

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

# Character set for auto-generation: lowercase letters + digits.
_SLUG_CHARS = string.ascii_lowercase + string.digits

# Length of auto-generated slugs (issue #981). 36^6 ≈ 2.2 billion slots.
_SLUG_LENGTH = 6

# Maximum attempts to generate a unique slug before giving up. In practice
# collisions are extremely rare; this guards against degenerate uniqueness states.
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
    """Return a fresh 6-character lowercase-alphanumeric slug not in ``taken``.

    Uses :func:`secrets.choice` to draw from ``[a-z0-9]`` for each character,
    producing a cryptographically random slug of length :data:`_SLUG_LENGTH`.
    Retries on collision (``slug in taken``) or when the candidate fails
    ``validate_slug`` (e.g. a vanishingly rare reserved-word hit) up to
    :data:`_MAX_ATTEMPTS` times, then raises
    :class:`~mc_server_dashboard_api.servers.domain.errors.SlugExhaustedError`.

    The caller passes the **deployment-wide** set of already-taken slugs (read
    from the repository inside the same transaction that will insert the new
    server row) so the uniqueness guarantee is transaction-local; the database
    ``UNIQUE`` constraint is the ultimate backstop.
    """

    for _ in range(_MAX_ATTEMPTS):
        candidate = "".join(secrets.choice(_SLUG_CHARS) for _ in range(_SLUG_LENGTH))
        try:
            validate_slug(candidate)
        except InvalidSlugError:
            continue
        if candidate not in taken:
            return candidate
    raise SlugExhaustedError()
