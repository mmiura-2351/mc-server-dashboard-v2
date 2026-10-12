/** Interpolate path parameters while retaining the template's OpenAPI response type. */

import type { paths } from "./schema";

/** Derive required string parameters from template segments, e.g. /a/{x} becomes { x: string }. */
export type PathParams<P extends string> =
  P extends `${string}{${infer Name}}${infer Rest}`
    ? { [K in Name | keyof PathParams<Rest>]: string }
    : Record<never, never>;

/** Encode each parameter and retain the template type for response inference. */
export function apiPath<P extends keyof paths & string>(
  template: P,
  params: PathParams<P>,
): P {
  return template.replace(/\{([^}]+)\}/g, (_match, name: string) =>
    encodeURIComponent((params as Record<string, string>)[name]),
  ) as P;
}
