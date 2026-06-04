"""Test the bundled common-password blocklist loader."""

from __future__ import annotations

from mc_server_dashboard_api.identity.adapters.common_passwords import (
    load_common_passwords,
)


def test_loads_blocklist_case_folded() -> None:
    blocklist = load_common_passwords()
    # A few entries from the bundled SecLists top list, normalized to casefold.
    assert "password" in blocklist
    assert "123456" in blocklist
    # Membership is case-folded, so an upper-cased spelling resolves too.
    assert "PASSWORD".casefold() in blocklist


def test_blocklist_is_substantial() -> None:
    # The bundled list is the SecLists 10k-most-common set; sanity-check size.
    assert len(load_common_passwords()) > 9000
