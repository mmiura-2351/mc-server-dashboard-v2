"""Tests for the hermetic OpenAPI schema export (webui client generation).

The export must build the app and serialise ``app.openapi()`` without a
database, network, or any ``MCD_API_*`` environment variables — the placeholders
it supplies are enough to pass the app factory's fail-fast checks. The env vars
the test conftest sets are cleared here to prove the export stands on its own.
"""

import json
from pathlib import Path

import pytest

from mc_server_dashboard_api.export_openapi import export, main


@pytest.fixture(autouse=True)
def _clear_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MCD_API_DATABASE__URL",
        "MCD_API_AUTH__TOKEN__SIGNING_KEY",
        "MCD_API_CONTROL__ENABLED",
        "MCD_API_CONTROL__TLS__INSECURE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_export_writes_openapi_schema(tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"

    export(out)

    schema = json.loads(out.read_text(encoding="utf-8"))
    assert schema["openapi"].startswith("3.")
    assert "/auth/login" in schema["paths"]
    assert "/communities" in schema["paths"]


def test_export_output_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    export(first)
    export(second)

    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_main_requires_exactly_one_argument() -> None:
    assert main([]) == 2
    assert main(["a", "b"]) == 2
