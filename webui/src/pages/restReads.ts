/**
 * Orders live WS states against the REST reads of a query (#3213).
 *
 * A REST response can land after a WS frame that is newer than the response's
 * read: the cold load racing the subscribe snapshot, or a reopen/gap refetch
 * racing the frames that follow it. Writing the response over the cache would
 * then roll a live state back. One logical clock orders the two: a live state
 * is stamped when it is received ({@link stamp}), a REST read when its request
 * starts ({@link stampedQueryFn}). When a response lands, a live state stamped
 * after that request started is newer than it and must be re-applied on top;
 * one stamped before is superseded by the response, which can carry a change
 * no frame announced.
 *
 * The stamp is taken inside the query function, so every attempt counts —
 * retries and restarted fetches included. The response that lands is always
 * the latest attempt's: TanStack discards a cancelled fetch's result, and
 * retries run one after another.
 */

import {
  hashKey,
  type QueryClient,
  type QueryFunction,
  type QueryKey,
} from "@tanstack/react-query";

let clock = 0;

/** The stamp of the latest query-function attempt, by query hash. */
const attemptStarts = new Map<string, number>();

/** Stamp a live state received now. */
export function stamp(): number {
  return ++clock;
}

/**
 * Wrap a query function so each attempt's start is stamped. The query whose
 * cache a live-events hook patches must use it, or {@link observeRestResponses}
 * treats every response as older than every live state.
 */
export function stampedQueryFn<T, K extends QueryKey>(
  fn: QueryFunction<T, K>,
): QueryFunction<T, K> {
  return (context) => {
    attemptStarts.set(hashKey(context.queryKey), ++clock);
    return fn(context);
  };
}

/**
 * Run `onResponse` after each REST response for `queryKey` has been written to
 * the cache, with the stamp of the attempt that produced it. Returns the
 * unsubscribe function.
 */
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
      // `manual` marks a `setQueryData` write (ours included), not a response.
      !event.action.manual
    ) {
      onResponse(attemptStarts.get(hash) ?? 0);
    }
  });
}
