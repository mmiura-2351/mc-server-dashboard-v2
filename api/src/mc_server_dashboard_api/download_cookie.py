"""The download cookie: the credential that survives an interrupted transfer.

A download grant travels in the query string — the only credential a browser
navigation carries — so its TTL *is* its exposure: it lands in reverse-proxy
access logs, in browser history, and in any ``Referer``. That is why it is 30 s by
default (issue #2313), which is shorter than any large transfer. A browser
retrying an interrupted download re-presents the same URL, so the retry arrived
with an expired grant and got a 401 — which Chrome renders as "Sign in to the
site, then try downloading again" on a page the user is already signed into
(issue #2373).

Redeeming a grant therefore *also* mints a cookie carrying the same
resource-scoped authority on a longer TTL. The query-string window is left exactly
as it was, and the credential that outlives the transfer is one no log, history
entry or ``Referer`` can leak. Its attributes:

- **``HttpOnly``** — no script reads it, in the SPA or in an injected one.
- **``Secure``** — HTTPS only (``auth.token.download_cookie_secure``; turn it off
  for plain-HTTP localhost dev, or the browser stores nothing and every resume
  silently falls back to the 401 this exists to fix).
- **``SameSite=Strict``** — never attached to a cross-site request. A download is
  started by clicking an ``<a download>`` at a same-origin URL, so both it and the
  retry the browser issues for it are same-site and do carry the cookie. (If a
  browser is ever found to classify its download manager's *resume* request as
  cross-site, ``Lax`` is the sufficient fallback: this cookie only ever authorizes
  a ``GET`` of the one resource it names.)
- **``Path``** = the download's own URL path, so the browser attaches it to
  nothing else. That is defence in depth, *not* the authorization: the cookie
  names its resource in a signed claim, and ``require_download_access`` verifies
  that claim against the resource actually being fetched. Its authority is one
  resource whatever path a client chooses to send it to.
- **``Max-Age``** = ``auth.token.download_cookie_ttl_seconds``. The browser's
  copy expiring is a courtesy; the JWT's own ``exp`` is what is enforced.

Only a *grant-authenticated* request mints one. A Bearer client gets no
``Set-Cookie`` it never asked for (the posture issue #372 set for the refresh
cookie), and a request that authenticated *with* the cookie does not re-mint it —
no sliding window, so a cookie's life is bounded absolutely by the redemption
that minted it.

The mint lands here rather than on the ``Response`` a dependency can declare:
all three download routes **return** their ``StreamingResponse``, and FastAPI
merges a dependency sub-response's headers only into a response it builds itself,
so a ``Set-Cookie`` set there is silently dropped. The gate records the pending
cookie on ``request.state`` and this middleware stamps it on the way out — one
mechanism for every route the gate covers, with no per-route line to forget.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal

from starlette.requests import Request
from starlette.responses import Response

# One fixed name for all three downloads: distinct ``Path`` scopes already keep
# one resource's cookie out of another's request, and a per-resource name would
# instead pile every same-path resource's cookie into one ``Cookie`` header.
DOWNLOAD_COOKIE_NAME: Final = "mcd_dl"

_SAMESITE: Literal["strict"] = "strict"

# Where the gate parks the mint for the middleware to stamp. Private: the two
# sides talk through :func:`remember_download_cookie` only.
_STATE_ATTR: Final = "pending_download_cookie"


@dataclass(frozen=True)
class _PendingDownloadCookie:
    value: str
    path: str
    max_age: int
    secure: bool


def remember_download_cookie(
    request: Request, *, value: str, path: str, max_age: int, secure: bool
) -> None:
    """Record the cookie :func:`download_cookie_middleware` should stamp."""

    setattr(
        request.state,
        _STATE_ATTR,
        _PendingDownloadCookie(value=value, path=path, max_age=max_age, secure=secure),
    )


async def download_cookie_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Stamp the download cookie a redeemed grant minted (issue #2373).

    Only on a 2xx: a request whose gate passed but whose route then answered 404
    or 409 ``server_unsettled`` hands out no credential for a resource it did not
    serve. A response that does carry the cookie is also marked ``no-store``, so
    no shared cache can replay the credential to a second client.
    """

    response = await call_next(request)
    pending: _PendingDownloadCookie | None = getattr(request.state, _STATE_ATTR, None)
    if pending is not None and 200 <= response.status_code < 300:
        response.set_cookie(
            key=DOWNLOAD_COOKIE_NAME,
            value=pending.value,
            max_age=pending.max_age,
            path=pending.path,
            httponly=True,
            secure=pending.secure,
            samesite=_SAMESITE,
        )
        # A response carrying a credential is never stored (RFC 6265 Section 8.6):
        # a shared cache could otherwise hand this Set-Cookie to a second client.
        # All three download routes declare ``no-store`` themselves (issue #2491),
        # which already covers every response reached today -- do not delete this
        # as redundant: it is what holds for any future route the gate mints a
        # cookie on, whose own declaration is one line for a reader to forget.
        response.headers["Cache-Control"] = "no-store"
    return response
