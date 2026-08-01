"""Tests for the body-less HEAD response of the download routes (issue #2383).

The three download edges build a header dict and hand it to
:func:`head_response`; what this module owes them is that the dict arrives on the
wire unchanged, with no body and — the trap it exists for — no ``Content-Length``
invented for the empty one.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.http_head import head_response


def _client(headers: dict[str, str]) -> TestClient:
    app = FastAPI()

    @app.head("/probe")
    async def probe() -> object:
        return head_response(media_type="application/zip", headers=headers)

    return TestClient(app)


def test_head_response_sends_the_given_headers_and_no_body() -> None:
    client = _client({"Content-Length": "42", "ETag": '"abc"'})

    resp = client.head("/probe")

    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-length"] == "42"
    assert resp.headers["etag"] == '"abc"'
    assert resp.headers["content-type"] == "application/zip"


def test_head_response_declares_no_length_it_was_not_given() -> None:
    # A plain Response would answer ``Content-Length: 0`` here, telling a probe
    # of an incrementally built zip that the representation is empty.
    client = _client({"Cache-Control": "no-store"})

    resp = client.head("/probe")

    assert resp.status_code == 200
    assert "content-length" not in resp.headers
