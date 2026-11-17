"""Constant-time comparison of a presented credential (issue #3695)."""

from __future__ import annotations

import hmac


def credential_matches(presented: str, expected: str) -> bool:
    """Whether ``presented`` equals ``expected``, compared in constant time.

    The compare is over the UTF-8 bytes: ``hmac.compare_digest`` refuses a
    ``str`` holding a non-ASCII character, and gRPC metadata and proto strings
    can carry one. A value with no UTF-8 encoding (a lone surrogate, as an
    undecodable environment byte becomes) matches nothing, so the caller
    rejects it as it rejects any wrong credential rather than raising.
    """

    try:
        return hmac.compare_digest(presented.encode(), expected.encode())
    except UnicodeEncodeError:
        return False
