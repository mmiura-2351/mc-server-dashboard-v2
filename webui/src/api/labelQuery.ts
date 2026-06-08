/**
 * Graceful degradation for display-only secondary reads (issue #471).
 *
 * A tab gated on one permission (its primary read) often issues secondary reads
 * under *different* gates only to resolve ids into display names — e.g. the
 * Grants tab (`grant:read`) fetches members (`member:read`) and servers
 * (`server:read`) just to label rows. A caller holding only the primary gate
 * gets a 403 on those secondary reads, which would otherwise collapse the whole
 * tab into its generic load error.
 *
 * `labelQueryFn` wraps such a `queryFn` so that a 403 (and *only* a 403)
 * resolves to a supplied empty value instead of rejecting. The row-rendering
 * code then falls back to raw ids. Every other failure — 404, 500, a network
 * error — still rejects so a real outage is never hidden, and the primary read
 * (which is not wrapped) still fails the tab as before.
 *
 * The empty value is returned per call, not cached specially: React Query stores
 * it like any success, so once the caller gains the secondary gate a normal
 * staleness-driven refetch replaces it with full labels.
 */

import { ApiError } from "./client.ts";

/**
 * Wrap a label-resolution `queryFn` to swallow a 403 into `emptyValue`. Use only
 * for display-only secondary reads; never for the tab's primary read.
 */
export function labelQueryFn<T>(
  queryFn: () => Promise<T>,
  emptyValue: T,
): () => Promise<T> {
  return async () => {
    try {
      return await queryFn();
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) {
        return emptyValue;
      }
      throw error;
    }
  };
}
