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
import { ApiError, getRefresher } from "./client.ts";

/** Maximum download size (512 MiB), matching the upload limit. */
export const MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024;

/**
 * Thrown when a download exceeds {@link MAX_DOWNLOAD_BYTES}. Callers show a
 * user-facing message with the reported size.
 */
export class DownloadTooLargeError extends Error {
  readonly size: number;
  constructor(size: number) {
    super(`Download too large: ${size} bytes`);
    this.name = "DownloadTooLargeError";
    this.size = size;
  }
}

/**
 * Fetch `path` with the current access token and save the response as a file
 * named `filename`. Throws {@link ApiError} on a non-2xx response so callers can
 * branch on `status` (403 → permission glue, 409 → state-changed) exactly like
 * the JSON client.
 */
async function fetchDownload(
  path: string,
  signal?: AbortSignal,
): Promise<Response> {
  const token = getAccessToken();
  return fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: token != null ? { authorization: `Bearer ${token}` } : {},
    signal,
  });
}

/**
 * True for the `AbortError` DOMException fetch rejects with when its signal
 * aborts (issue #1728). Callers use it to swallow intentional cancellations
 * instead of surfacing them as user-visible errors. Matched by name, not
 * `instanceof`: not every runtime puts `Error.prototype` on DOMException's
 * chain (jsdom does not).
 */
export function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

/**
 * Fetch a file as a {@link Blob} with auth + transparent 401 refresh.
 * Used by {@link downloadFile} for single-file saves and by the bulk-download
 * ZIP builder in `ServerFilesTab`. An aborted `signal` rejects with the
 * fetch `AbortError` (see {@link isAbortError}).
 */
export async function fetchFileBlob(
  path: string,
  signal?: AbortSignal,
): Promise<Blob> {
  let response = await fetchDownload(path, signal);

  // Transparent refresh: a 401 means the access token expired. Run the shared
  // single-flight refresh (registered by the session layer) and retry once,
  // mirroring the JSON client's behaviour (client.ts:164-172).
  const currentRefresher = getRefresher();
  if (response.status === 401 && currentRefresher !== null) {
    const refreshed = await currentRefresher();
    if (refreshed) {
      response = await fetchDownload(path, signal);
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, await readProblem(response));
  }

  // Guard against OOM: reject downloads that exceed the size cap before
  // buffering the body. The check is best-effort — some responses omit
  // Content-Length (e.g. chunked transfers) and will pass through.
  const cl = response.headers.get("content-length");
  if (cl !== null) {
    const size = Number(cl);
    if (size > MAX_DOWNLOAD_BYTES) {
      // Discard the body so the connection can be reused.
      await response.body?.cancel();
      throw new DownloadTooLargeError(size);
    }
  }

  return response.blob();
}

export async function downloadFile(
  path: string,
  filename: string,
  signal?: AbortSignal,
): Promise<void> {
  const blob = await fetchFileBlob(path, signal);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Defer the revoke so the click-initiated download has the object URL when it
  // actually fetches; revoking synchronously can race the save in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 0);
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
