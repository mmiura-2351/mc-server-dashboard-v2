/**
 * Authenticated file download via fetch + blob (WEBUI_SPEC.md 7.1).
 *
 * The API authenticates with a Bearer access token only (no cookie auth, no
 * query-token path) and the token lives in memory, never in storage. So a plain
 * `<a href>` / `window.location` to a streamed-download endpoint (e.g. the
 * server export ZIP) cannot carry the credential — the request would arrive
 * unauthenticated. The honest path is to fetch the URL with the Authorization
 * header set, read the response as a Blob, and click a synthesised anchor at an
 * object URL so the browser saves the file.
 *
 * This is intentionally separate from the JSON {@link api} client: that client
 * parses every body as JSON, which a binary ZIP is not. The trade-off is that
 * the whole archive buffers in memory before the save prompt; for the working
 * sets these endpoints serve that is acceptable, and it is the only way to
 * attach the in-memory token (issue #438).
 */

import { getAccessToken } from "../auth/tokenStore.ts";
import { ApiError } from "./client.ts";

/**
 * Fetch `path` with the current access token and save the response as a file
 * named `filename`. Throws {@link ApiError} on a non-2xx response so callers can
 * branch on `status` (403 → permission glue, 409 → state-changed) exactly like
 * the JSON client.
 */
export async function downloadFile(
  path: string,
  filename: string,
): Promise<void> {
  const token = getAccessToken();
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: token != null ? { authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) {
    throw new ApiError(response.status, await readProblem(response));
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/**
 * Best-effort parse of an error body as RFC 9457 problem+json so the thrown
 * {@link ApiError} carries `reason` (e.g. 409 server_unsettled). A non-JSON or
 * unreadable body yields `undefined`, matching the JSON client's posture.
 */
async function readProblem(response: Response): Promise<unknown> {
  try {
    const text = await response.text();
    return text.length > 0 ? JSON.parse(text) : undefined;
  } catch {
    return undefined;
  }
}
