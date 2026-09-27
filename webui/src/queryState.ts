/**
 * Classify the rendering state of a TanStack Query result.
 *
 * A failed background refetch retains the last successful `data`, so available
 * data takes precedence over `isError`. Only an error without data needs an
 * error surface; a result with neither is still initially pending.
 */
export type QueryResultState<T> =
  | { kind: "pending" }
  | { kind: "error" }
  | { kind: "data"; data: T };

export function classifyQueryResult<T>({
  data,
  isError,
}: {
  data: T | undefined;
  isError: boolean;
}): QueryResultState<T> {
  if (data !== undefined) {
    return { kind: "data", data };
  }
  return isError ? { kind: "error" } : { kind: "pending" };
}
