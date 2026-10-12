/** Use same-origin URLs and in-memory Bearer tokens; the session layer injects 401 refresh handling. */

import { getAccessToken, getAuthEpoch } from "../auth/tokenStore.ts";
import type { paths } from "./schema";

type PathsWith<M extends string> = {
  [P in keyof paths]: paths[P] extends Record<M, unknown> ? P : never;
}[keyof paths];

/** OpenAPI emits successful status codes as literal keys. */
type SuccessStatus = 200 | 201 | 202 | 203 | 204 | 205 | 206;

type JsonResponse<Op> = Op extends { responses: infer R }
  ? R[Extract<keyof R, SuccessStatus>] extends infer Success
    ? Success extends { content: { "application/json": infer B } }
      ? B
      : undefined
    : unknown
  : unknown;

type Op<P extends keyof paths, M extends string> = M extends keyof paths[P]
  ? paths[P][M]
  : never;

/** RFC 9457 extension fields used by the UI. */
interface ProblemDetails {
  reason?: string;
  permission?: string;
  key?: string;
}

function readProblemString(
  body: unknown,
  key: keyof ProblemDetails,
): string | undefined {
  if (typeof body === "object" && body !== null) {
    const value = (body as ProblemDetails)[key];
    if (typeof value === "string") {
      return value;
    }
  }
  return undefined;
}

export class ApiError extends Error {
  /** RFC 9457 reason extension, when supplied as a string. */
  readonly reason: string | undefined;
  /** Missing permission code on authorization denials, when supplied as a string. */
  readonly permission: string | undefined;
  /** Platform-managed server.properties key rejected by the API, when supplied as a string. */
  readonly key: string | undefined;

  constructor(
    readonly status: number,
    readonly body: unknown,
  ) {
    super(`API request failed with status ${status}`);
    this.name = "ApiError";
    this.reason = readProblemString(body, "reason");
    this.permission = readProblemString(body, "permission");
    this.key = readProblemString(body, "key");
  }
}

/** Inject refresh handling to avoid a client/session import cycle.
 * A successful refresh is valid only in its request epoch; recheck before retrying. */
type Refresher = (requestEpoch: number) => Promise<boolean>;
let refresher: Refresher | null = null;

export function setRefresher(fn: Refresher): void {
  refresher = fn;
}

export function getRefresher(): Refresher | null {
  return refresher;
}

export function resetForTesting(): void {
  refresher = null;
}

function isAuthPath(path: string): boolean {
  return path.startsWith("/api/auth/");
}

/** Proxy error pages may be HTML; parse only the API's JSON content types. */
function isJsonContentTypeHeader(contentType: string | null): boolean {
  return /\bapplication\/(problem\+)?json\b/i.test(contentType ?? "");
}

function isJsonContentType(response: Response): boolean {
  return isJsonContentTypeHeader(response.headers.get("content-type"));
}

async function rawRequest(
  method: string,
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const token = getAccessToken();
  // Let the browser supply a multipart boundary for FormData; other bodies use JSON.
  const isFormData = init?.body instanceof FormData;
  return fetch(path, {
    ...init,
    method,
    credentials: "same-origin",
    headers: {
      ...(init?.body != null && !isFormData
        ? { "content-type": "application/json" }
        : {}),
      ...(token != null ? { authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });
}

async function request<P extends keyof paths, M extends string>(
  method: M,
  path: P,
  init?: RequestInit,
): Promise<JsonResponse<Op<P, M>>> {
  const httpMethod = method.toUpperCase();
  const url = path as string;

  // Capture the epoch synchronously with rawRequest reading the token.
  const epoch = getAuthEpoch();
  let response = await rawRequest(httpMethod, url, init);

  // Retry non-auth 401s once, only within the original session; refreshing auth routes would recurse.
  if (response.status === 401 && refresher !== null && !isAuthPath(url)) {
    const refreshed = await refresher(epoch);
    if (refreshed && epoch === getAuthEpoch()) {
      response = await rawRequest(httpMethod, url, init);
    }
    // Surface a second 401 as ApiError without another refresh or hard logout.
  }

  // Preserve the HTTP status when a proxy returns HTML or malformed JSON.
  const text = await response.text();
  const jsonBody = isJsonContentType(response);
  let body: unknown;
  if (jsonBody && text.length > 0) {
    try {
      body = JSON.parse(text);
    } catch {
      // Malformed JSON falls through to the ApiError paths below.
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, body);
  }

  // A nonempty success body that cannot be parsed is also an ApiError.
  if (text.length > 0 && body === undefined) {
    throw new ApiError(response.status, undefined);
  }
  return body as JsonResponse<Op<P, M>>;
}

/** Reports upload progress in bytes; `total` is 0 when the size is unknown. */
export type UploadProgress = (loaded: number, total: number) => void;

/** Match AbortError by name so intentional upload cancellation can be handled across runtimes. */
export function isUploadAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

/** Resolve on any HTTP response; reject transport failures and signal cancellation.
 * The caller handles status codes and parses the response. */
function sendForm(
  url: string,
  body: FormData,
  onProgress?: UploadProgress,
  signal?: AbortSignal,
): Promise<XMLHttpRequest> {
  if (signal?.aborted) {
    return Promise.reject(
      new DOMException("The operation was aborted.", "AbortError"),
    );
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.withCredentials = true;
    const token = getAccessToken();
    if (token != null) {
      xhr.setRequestHeader("authorization", `Bearer ${token}`);
    }
    // Let the browser set the multipart boundary.
    if (onProgress !== undefined) {
      xhr.upload.onprogress = (event) => {
        onProgress(event.loaded, event.lengthComputable ? event.total : 0);
      };
    }
    xhr.onload = () => resolve(xhr);
    xhr.onerror = () => reject(new ApiError(0, undefined));
    xhr.onabort = () =>
      reject(new DOMException("The operation was aborted.", "AbortError"));
    if (signal !== undefined) {
      signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }
    xhr.send(body);
  });
}

/** Use XHR because fetch does not expose upload progress; retain the JSON client's auth and error handling. */
export async function postFormWithProgress<P extends PathsWith<"post">>(
  path: P,
  body: FormData,
  onProgress?: UploadProgress,
  signal?: AbortSignal,
): Promise<JsonResponse<Op<P, "post">>> {
  const url = path as string;
  const epoch = getAuthEpoch();
  let xhr = await sendForm(url, body, onProgress, signal);

  // Retry a 401 upload only while its original session is current.
  if (xhr.status === 401 && refresher !== null && !isAuthPath(url)) {
    const refreshed = await refresher(epoch);
    if (refreshed && epoch === getAuthEpoch()) {
      xhr = await sendForm(url, body, onProgress, signal);
    }
  }

  const text = xhr.responseText;
  const jsonBody = isJsonContentTypeHeader(
    xhr.getResponseHeader("content-type"),
  );
  let parsed: unknown;
  if (jsonBody && text.length > 0) {
    try {
      parsed = JSON.parse(text);
    } catch {
      // Malformed JSON falls through to the ApiError paths below.
    }
  }

  const ok = xhr.status >= 200 && xhr.status < 300;
  if (!ok) {
    throw new ApiError(xhr.status, parsed);
  }
  if (text.length > 0 && parsed === undefined) {
    throw new ApiError(xhr.status, undefined);
  }
  return parsed as JsonResponse<Op<P, "post">>;
}

export const api = {
  get: <P extends PathsWith<"get">>(path: P, init?: RequestInit) =>
    request("get", path, init),
  post: <P extends PathsWith<"post">>(path: P, init?: RequestInit) =>
    request("post", path, init),
  put: <P extends PathsWith<"put">>(path: P, init?: RequestInit) =>
    request("put", path, init),
  patch: <P extends PathsWith<"patch">>(path: P, init?: RequestInit) =>
    request("patch", path, init),
  delete: <P extends PathsWith<"delete">>(path: P, init?: RequestInit) =>
    request("delete", path, init),
};
