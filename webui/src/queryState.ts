/** Keep available data after a failed background refetch; show an error only when no data exists. */
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
