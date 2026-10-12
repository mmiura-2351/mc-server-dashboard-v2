import { type ComponentType, lazy } from "react";

/** Retry a failed chunk import once; cached HTML may reference a chunk removed by a deployment. */
export function lazyRetry<T extends ComponentType<unknown>>(
  factory: () => Promise<{ default: T }>,
): React.LazyExoticComponent<T> {
  return lazy(() =>
    factory().catch(() =>
      // Let a second import failure reach the error boundary.
      factory(),
    ),
  );
}
