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
 */

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

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: unknown,
  ) {
    super(`API request failed with status ${status}`);
    this.name = "ApiError";
  }
}

async function request<P extends keyof paths, M extends string>(
  method: M,
  path: P,
  init?: RequestInit,
): Promise<JsonResponse<Op<P, M>>> {
  const response = await fetch(path as string, {
    ...init,
    method: method.toUpperCase(),
    headers: {
      ...(init?.body != null ? { "content-type": "application/json" } : {}),
      ...init?.headers,
    },
  });
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
