/** Fetch single files with the in-memory Bearer token and cap buffering at MAX_DOWNLOAD_BYTES.
 * Large archive downloads use minted URLs and saveUrlAs to stream directly to disk. */

import { getAccessToken, getAuthEpoch } from "../auth/tokenStore.ts";
import { ApiError, getRefresher } from "./client.ts";

/** Cap buffered downloads at 512 MiB. */
export const MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024;

/** Carries the rejected download size so callers can explain the limit. */
export class DownloadTooLargeError extends Error {
  readonly size: number;
  constructor(size: number) {
    super(`Download too large: ${size} bytes`);
    this.name = "DownloadTooLargeError";
    this.size = size;
  }
}

/** Non-2xx responses remain ApiError instances for the caller's permission and state handling. */
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

/** Match AbortError by name; DOMException does not extend Error in every runtime. */
export function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

/** Fetch a capped Blob for single-file saves or bulk ZIP assembly; aborts propagate as AbortError. */
export async function fetchFileBlob(
  path: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const epoch = getAuthEpoch();
  let response = await fetchDownload(path, signal);

  // Refresh and retry once, only while the download's original session is current.
  const currentRefresher = getRefresher();
  if (response.status === 401 && currentRefresher !== null) {
    const refreshed = await currentRefresher(epoch);
    if (refreshed && epoch === getAuthEpoch()) {
      response = await fetchDownload(path, signal);
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, await readProblem(response));
  }

  // Reject a declared oversized body before buffering any bytes.
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

/** Count streamed bytes as well as checking Content-Length; chunked responses may omit it. */
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
      // Cancel the remaining stream when the buffered size exceeds the cap.
      await reader.cancel();
      throw new DownloadTooLargeError(received);
    }
    chunks.push(value);
  }
  return new Blob(chunks, {
    type: response.headers.get("content-type") ?? "",
  });
}

/** The download attribute requires a same-origin URL. */
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
  // Defer revocation so the browser can read the object URL after the anchor click.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** Preserve problem details when available; unreadable error bodies still produce ApiError. */
async function readProblem(response: Response): Promise<unknown> {
  try {
    const text = await response.text();
    return text.length > 0 ? JSON.parse(text) : undefined;
  } catch {
    return undefined;
  }
}
