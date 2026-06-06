/**
 * Typed path-parameter interpolation over the generated OpenAPI {@link paths}.
 *
 * Templated paths in the schema carry their parameters as `{name}` segments
 * (e.g. `/communities/{community_id}/me/permissions`). Call sites need the
 * concrete URL but must keep the literal path type so the {@link api} helpers
 * stay typed against the schema. {@link apiPath} does both: it substitutes the
 * params and returns a value typed as the original template path `P`, so no
 * `as` cast is needed at the call site.
 *
 * The param object is type-derived from the template by {@link PathParams}, so
 * a wrong or missing param name is a compile error — the interpolation stays
 * honest against the path it claims to produce.
 */

import type { paths } from "./schema";

/**
 * The names of the `{param}` segments in a path template, as a record of
 * required string values. `/a/{x}/b/{y}` yields `{ x: string; y: string }`;
 * a template with no params yields `{}`.
 */
export type PathParams<P extends string> =
  P extends `${string}{${infer Name}}${infer Rest}`
    ? { [K in Name | keyof PathParams<Rest>]: string }
    : Record<never, never>;

/**
 * Interpolate the `{param}` segments of a schema path template, URL-encoding
 * each value, and return the concrete URL typed as the template path `P` so the
 * generated response typing is preserved without a cast.
 */
export function apiPath<P extends keyof paths & string>(
  template: P,
  params: PathParams<P>,
): P {
  return template.replace(/\{([^}]+)\}/g, (_match, name: string) =>
    encodeURIComponent((params as Record<string, string>)[name]),
  ) as P;
}
