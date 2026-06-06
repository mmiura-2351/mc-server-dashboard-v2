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

function isAuthPath(path: string): boolean {
  return path.startsWith("/auth/");
}

async function rawRequest(
  method: string,
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const token = getAccessToken();
  return fetch(path, {
    ...init,
    method,
    credentials: "same-origin",
    headers: {
      ...(init?.body != null ? { "content-type": "application/json" } : {}),
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
  }

  const text = await response.text();
  const body: unknown = text.length > 0 ? JSON.parse(text) : undefined;
  if (!response.ok) {
    throw new ApiError(response.status, body);
  }
  return body as JsonResponse<Op<P, M>>;
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
