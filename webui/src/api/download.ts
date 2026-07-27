/**
 * Authenticated file download via fetch + blob (WEBUI_SPEC.md 7.1).
 *
 * These endpoints authenticate with a Bearer access token that lives in memory,
 * never in storage, so a plain `<a href>` / `window.location` to a
 * streamed-download endpoint (e.g. a single working-set file) cannot carry the
 * credential — the request would arrive unauthenticated. The honest path is to
 * fetch the URL with the Authorization header set, read the response as a Blob,
 * and click a synthesised anchor at an object URL so the browser saves the file.
 *
 * This is intentionally separate from the JSON {@link api} client: that client
 * parses every body as JSON, which a binary ZIP is not. The trade-off is that
 * the whole archive buffers in memory before the save prompt; for the working
 * sets these endpoints serve that is acceptable, and it is the only way to
 * attach the in-memory token (issue #438).
 *
 * The multi-GB archives are the exceptions, and no longer take this path:
 * buffering them hit {@link MAX_DOWNLOAD_BYTES} and failed. Backup archives
 * (#2314), the server export ZIP (#2353) and a directory's streamed ZIP (#2354)
 * each mint a short-lived self-authenticating URL instead
 * (`POST …/backups/{id}/download-grant`, #2313; `POST …/export/download-grant`
 * and `POST …/files/download-grant?path=…`, #2352) and hand it to
 * {@link saveUrlAs}, so the browser streams the bytes to disk without the tab
 * reading them. A single file stays here: the API declares its `Content-Length`,
 * so an oversize one is rejected before a byte is read.
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

  // Guard against OOM: reject a download that exceeds the size cap before
  // buffering it. A declared Content-Length rejects up front, without reading
  // a byte.
  const cl = response.headers.get("content-length");
  if (cl !== null) {
    const size = Number(cl);
    if (size > MAX_DOWNLOAD_BYTES) {
      // Discard the body so the connection can be reused.
      await response.body?.cancel();
      throw new DownloadTooLargeError(size);
    }
  }

  return readCappedBlob(response);
}

/**
 * Buffer `response`'s body as a Blob, aborting past {@link MAX_DOWNLOAD_BYTES}.
 *
 * The header check above cannot stand alone: a chunked response declares no
 * length, so it would otherwise buffer unbounded. That is not a corner case —
 * the API streams a directory download as a zip built incrementally, with no
 * Content-Length at all (`files.py` `download_file`), so a multi-GB `world`
 * folder took exactly this path (issue #2027). Counting bytes as they arrive
 * caps every response, whatever it declares, at the cost of one extra copy
 * (the chunk list) over `response.blob()`.
 */
async function readCappedBlob(response: Response): Promise<Blob> {
  if (response.body === null) return response.blob();

  const reader = response.body.getReader();
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.byteLength;
    if (received > MAX_DOWNLOAD_BYTES) {
      // Stop pulling and release the connection: the bytes already read are
      // dropped with `chunks`, and the rest is never transferred.
      await reader.cancel();
      throw new DownloadTooLargeError(received);
    }
    chunks.push(value);
  }
  return new Blob(chunks, {
    type: response.headers.get("content-type") ?? "",
  });
}

/**
 * Save `url` as a file named `filename` by clicking a synthesised anchor.
 *
 * The `download` attribute only applies same-origin, which every URL handed
 * here is (WEBUI_SPEC.md 7.7). The anchor is detached again immediately; the
 * click leaves no history entry.
 */
export function saveUrlAs(url: string, filename: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

export async function downloadFile(
  path: string,
  filename: string,
  signal?: AbortSignal,
): Promise<void> {
  const blob = await fetchFileBlob(path, signal);
  const url = URL.createObjectURL(blob);
  saveUrlAs(url, filename);
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
