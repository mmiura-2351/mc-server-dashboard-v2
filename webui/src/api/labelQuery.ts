/** Secondary label reads may fall back to raw IDs on 403; primary reads and other failures must propagate. */

import { ApiError } from "./client.ts";

/** Use only for secondary display labels: resolve 403 to emptyValue and forward query arguments unchanged. */
export function labelQueryFn<T, A extends unknown[]>(
  queryFn: (...args: A) => Promise<T>,
  emptyValue: T,
): (...args: A) => Promise<T> {
  return async (...args: A) => {
    try {
      return await queryFn(...args);
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) {
        return emptyValue;
      }
      throw error;
    }
  };
}
