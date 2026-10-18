/**
 * The app's shared {@link QueryClient} and its default retry policy (#3361).
 *
 * TanStack Query retries every failed query three times by default, whatever
 * the failure. A 4xx is the API's deterministic answer — a missing file, a
 * refused symlink, a denied permission — and asking again only delays the
 * message by the backoff (about 7 s). So a query retries only what can clear on
 * its own: network failures, 5xx, 408 and 429.
 */

import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "./api/client.ts";

/** TanStack Query's own default retry count, kept for retryable failures. */
const MAX_QUERY_RETRIES = 3;

export function shouldRetryQuery(
  failureCount: number,
  error: unknown,
): boolean {
  if (failureCount >= MAX_QUERY_RETRIES) {
    return false;
  }
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return error.status === 408 || error.status === 429;
  }
  return true;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: shouldRetryQuery } },
  });
}
