// Dev-proxy helpers. The Vite dev server proxies the API path prefixes
// (vite.config.ts) to the local API, but several of those prefixes are also
// SPA routes (e.g. /admin/workers, /communities/{id}). A hard refresh or
// direct load of such a URL is a browser navigation and must be served the
// SPA's index.html — not forwarded to the API — so deep links work in dev the
// same way the API's StaticFiles SPA fallback handles them in production
// (WEBUI_SPEC.md 7.7). API/fetch/WebSocket requests still proxy.

// True when the request is a top-level browser navigation: it accepts HTML and
// is not a WebSocket upgrade. fetch() calls with `Accept: application/json`
// and WS handshakes (which send `Upgrade: websocket`) return false and proxy.
export function isSpaNavigation(headers: {
  accept?: string | string[];
  upgrade?: string | string[];
}): boolean {
  if (headers.upgrade !== undefined) {
    return false;
  }
  const accept = headers.accept;
  const acceptValue = Array.isArray(accept) ? accept.join(",") : (accept ?? "");
  return acceptValue.includes("text/html");
}
