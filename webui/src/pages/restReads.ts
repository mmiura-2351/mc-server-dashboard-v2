/**
 * Orders live WS states against the REST reads of one query (#3213).
 *
 * A REST response can land after a WS frame that is newer than the response's
 * read: the cold load racing the subscribe snapshot, or a reopen/gap refetch
 * racing the frames that follow it. Writing the response over the cache would
 * then roll a live state back. This keeps one logical clock: a live state is
 * stamped when it is received, a REST read when it starts. When a response
 * lands, a live state stamped after its read started is newer than it and must
 * be re-applied on top; one stamped before is superseded by the response.
 *
 * Read starts are observed from the query cache (`fetch` actions) and stamped
 * by {@link RestReads.refetch} for the hook's own refetches, which may restart
 * an in-flight fetch without a `fetch` action.
 */

import {
  hashKey,
  type QueryClient,
  type QueryFunction,
  type QueryKey,
} from "@tanstack/react-query";

/** Wrap a query function so each attempt's start is stamped. */
export function stampedQueryFn<T, K extends QueryKey>(
  fn: QueryFunction<T, K>,
): QueryFunction<T, K> {
  return fn;
}

export interface RestReads {
  /** Stamp a live state received now; compare it to a read's start stamp. */
  stamp: () => number;
  /** Refetch the query, stamping the start of its read. */
  refetch: () => void;
  /** Stop observing the query. */
  close: () => void;
}

/**
 * Observe `queryKey`'s REST reads. `onResponse` runs after each REST response
 * has been written to the cache, with the stamp of the read's start.
 */
export function observeRestReads(
  queryClient: QueryClient,
  queryKey: QueryKey,
  onResponse: (readStartedAt: number) => void,
): RestReads {
  let clock = 0;
  let readStartedAt = 0;
  const hash = hashKey(queryKey);
  const close = queryClient.getQueryCache().subscribe((event) => {
    if (event.type !== "updated" || event.query.queryHash !== hash) {
      return;
    }
    if (event.action.type === "fetch") {
      readStartedAt = ++clock;
    } else if (event.action.type === "success" && !event.action.manual) {
      // `manual` marks a `setQueryData` write (ours included), not a response.
      onResponse(readStartedAt);
    }
  });
  return {
    stamp: () => ++clock,
    refetch: () => {
      readStartedAt = ++clock;
      queryClient.invalidateQueries({ queryKey });
    },
    close,
  };
}
