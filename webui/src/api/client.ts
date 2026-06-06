/**
 * Thin typed fetch helper over the generated OpenAPI {@link paths}.
 *
 * The UI is same-origin with the API (WEBUI_SPEC.md 7.7): in development the
 * Vite dev server proxies the API paths to a local API, in production the API
 * serves the built SPA. So requests use relative paths and the browser attaches
 * the same-origin session cookie automatically — there is no base URL and no
 * CORS here, by design.
 *
 * This is deliberately minimal (one request function); feature code in later
 * phases builds typed call sites on top of the generated `paths` type. It is
 * not a generated runtime — just enough to keep call sites honest against the
 * schema.
 *
 * On top of that it carries the session plumbing (WEBUI_SPEC.md 7.1): it
 * attaches the in-memory access token as a Bearer credential and, on a 401 from
 * a non-auth endpoint, transparently performs a single-flight refresh and
 * retries the request once. The refresh itself is owned by the session layer
 * and registered here, so the client does not depend on it.
 */

import { getAccessToken } from "../auth/tokenStore.ts";
import type { paths } from "./schema";

/** Paths that declare the given HTTP method in the generated schema. */
type PathsWith<M extends string> = {
  [P in keyof paths]: paths[P] extends Record<M, unknown> ? P : never;
}[keyof paths];

/** Successful (2xx) status codes openapi-typescript emits as literal keys. */
type SuccessStatus = 200 | 201 | 202 | 203 | 204 | 205 | 206;

/** The typed JSON response body for a successful (2xx) operation, if any. */
type JsonResponse<Op> = Op extends { responses: infer R }
  ? R[Extract<keyof R, SuccessStatus>] extends infer Success
    ? Success extends { content: { "application/json": infer B } }
      ? B
      : undefined
    : unknown
  : unknown;

/** The operation object for a given path + method. */
type Op<P extends keyof paths, M extends string> = M extends keyof paths[P]
  ? paths[P][M]
  : never;

/** Shape of an RFC 9457 `application/problem+json` body (AUTH_API.md 2). */
interface ProblemDetails {
  reason?: string;
}

/**
 * Pull the machine-readable `reason` out of a problem+json body. The UI
 * branches on exactly this one field (AUTH_API.md 2); anything that is not a
 * problem+json object yields `undefined`.
 */
function readReason(body: unknown): string | undefined {
  if (typeof body === "object" && body !== null) {
    const reason = (body as ProblemDetails).reason;
    if (typeof reason === "string") {
      return reason;
    }
  }
  return undefined;
}

export class ApiError extends Error {
  /** RFC 9457 `reason` extension member, when the body is problem+json. */
  readonly reason: string | undefined;

  constructor(
    readonly status: number,
    readonly body: unknown,
  ) {
    super(`API request failed with status ${status}`);
    this.name = "ApiError";
    this.reason = readReason(body);
  }
}

/**
 * The session layer registers a single-flight refresh here. It resolves true
 * when the access token was re-established and false on a hard logout, so the
 * client can decide whether to retry. Kept as an injected hook to avoid a
 * client -> session import cycle.
 */
type Refresher = () => Promise<boolean>;
let refresher: Refresher | null = null;

export function setRefresher(fn: Refresher): void {
  refresher = fn;
}

/**
 * Reset the injected refresher for tests. It is a module-level singleton that
 * otherwise survives across test cases/files; clearing it per case keeps the
 * 401-retry path from invoking a stale refresher left by an earlier render.
 */
export function resetForTesting(): void {
  refresher = null;
}

function isAuthPath(path: string): boolean {
  return path.startsWith("/auth/");
}

/**
 * Whether the response declares a JSON content-type the API uses: regular
 * `application/json` or RFC 9457 `application/problem+json` (AUTH_API.md 2).
 * Anything else (an HTML proxy/LB error page) is treated as non-JSON.
 */
function isJsonContentType(response: Response): boolean {
  const contentType = response.headers.get("content-type") ?? "";
  return /\bapplication\/(problem\+)?json\b/i.test(contentType);
}

async function rawRequest(
  method: string,
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const token = getAccessToken();
  // A FormData body is multipart: leave the content-type unset so the browser
  // adds it with the generated boundary. Everything else is JSON.
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

  let response = await rawRequest(httpMethod, url, init);

  // Transparent refresh: a 401 from a non-auth endpoint means the access token
  // expired. Run the shared single-flight refresh and retry once. The /auth/*
  // endpoints surface their own 401s untouched (no refresh on the refresh).
  if (response.status === 401 && refresher !== null && !isAuthPath(url)) {
    const refreshed = await refresher();
    if (refreshed) {
      response = await rawRequest(httpMethod, url, init);
    }
    // A 401 on the retried request (post-refresh) is intentionally surfaced as a
    // raw ApiError, not a hard logout: the session is valid, this endpoint just
    // denied access. The #410 guards must not misread it as session expiry.
  }

  // Only trust the body as JSON when the server says so. A non-JSON body — an
  // HTML 502/504 from the dev proxy or an LB error page — must surface as a
  // typed ApiError carrying the status, never as a raw SyntaxError leaking to
  // the caller.
  const text = await response.text();
  const jsonBody = isJsonContentType(response);
  let body: unknown;
  if (jsonBody && text.length > 0) {
    try {
      body = JSON.parse(text);
    } catch {
      // A JSON content-type whose body is unparseable: fall through to the
      // failure paths below with a body-less ApiError rather than throwing raw.
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, body);
  }

  // Success path stays strict: a 2xx that should carry JSON but did not (wrong
  // content-type or unparseable) is a typed failure, not a SyntaxError.
  if (text.length > 0 && body === undefined) {
    throw new ApiError(response.status, undefined);
  }
  return body as JsonResponse<Op<P, M>>;
}

export const api = {
  get: <P extends PathsWith<"get">>(path: P, init?: RequestInit) =>
    request("get", path, init),
  post: <P extends PathsWith<"post">>(path: P, init?: RequestInit) =>
    request("post", path, init),
  // Multipart POST: the only typed-client escape hatch for `multipart/form-data`
  // endpoints (server import / file & backup upload). Sends a FormData body
  // through the same refresh/error pipeline; the browser sets the boundary.
  postForm: <P extends PathsWith<"post">>(path: P, body: FormData) =>
    request("post", path, { body }),
  put: <P extends PathsWith<"put">>(path: P, init?: RequestInit) =>
    request("put", path, init),
  patch: <P extends PathsWith<"patch">>(path: P, init?: RequestInit) =>
    request("patch", path, init),
  delete: <P extends PathsWith<"delete">>(path: P, init?: RequestInit) =>
    request("delete", path, init),
};
