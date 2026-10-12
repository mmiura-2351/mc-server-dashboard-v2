/** Stamp REST attempts at request start and live states on receipt.
 * Reapply only live states newer than the response's attempt to prevent REST from overwriting newer socket data. */

import {
  hashKey,
  type QueryClient,
  type QueryFunction,
  type QueryKey,
} from "@tanstack/react-query";

let clock = 0;

const attemptStarts = new Map<string, number>();

export function stamp(): number {
  return ++clock;
}

/** Stamp every query attempt, including retries; unstamped responses appear older than every live state. */
export function stampedQueryFn<T, K extends QueryKey>(
  fn: QueryFunction<T, K>,
): QueryFunction<T, K> {
  return (context) => {
    attemptStarts.set(hashKey(context.queryKey), ++clock);
    return fn(context);
  };
}

/** Observe completed REST cache writes with the originating attempt's stamp; return an unsubscribe function. */
export function observeRestResponses(
  queryClient: QueryClient,
  queryKey: QueryKey,
  onResponse: (readStartedAt: number) => void,
): () => void {
  const hash = hashKey(queryKey);
  return queryClient.getQueryCache().subscribe((event) => {
    if (
      event.type === "updated" &&
      event.query.queryHash === hash &&
      event.action.type === "success" &&
      // Manual setQueryData writes are not REST responses.
      !event.action.manual
    ) {
      onResponse(attemptStarts.get(hash) ?? 0);
    }
  });
}
