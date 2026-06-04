"""Domain errors for the identity context.

Raised by the pure domain (value objects, entities) on invariant violations.
They carry no framework type and are translated to transport errors at the edge.
"""

from __future__ import annotations


class IdentityError(Exception):
    """Base class for identity-domain invariant violations."""


class InvalidUsernameError(IdentityError):
    """A username failed its validation rules (e.g. blank)."""


class InvalidEmailError(IdentityError):
    """An email address failed its validation rules (e.g. no ``@``)."""
