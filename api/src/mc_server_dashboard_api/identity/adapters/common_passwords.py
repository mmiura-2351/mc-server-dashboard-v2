"""Loader for the bundled common-password blocklist.

The blocklist is a packaged data file (``data/common_passwords.txt``) sourced
from SecLists ``Passwords/Common-Credentials/10k-most-common.txt`` (the
xato-net-derived 10,000 most-common passwords), one entry per line. Reading the
file is an adapter concern; the wiring layer loads it once and injects the
case-folded set into the pure :class:`PasswordPolicy` whenever the selected
``auth.password.policy`` preset screens the common-password list (every preset
does; CONFIGURATION.md Section 7.1).
"""

from __future__ import annotations

from importlib import resources

_DATA_PACKAGE = "mc_server_dashboard_api.identity.adapters.data"
_BLOCKLIST_FILE = "common_passwords.txt"


def load_common_passwords() -> frozenset[str]:
    """Return the bundled common passwords, case-folded for matching."""

    text = (
        resources.files(_DATA_PACKAGE)
        .joinpath(_BLOCKLIST_FILE)
        .read_text(encoding="utf-8")
    )
    return frozenset(line.casefold() for line in text.splitlines() if line.strip())
