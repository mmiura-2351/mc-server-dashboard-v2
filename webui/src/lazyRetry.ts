import { type ComponentType, lazy } from "react";

/**
 * Wrapper around `React.lazy` that retries a failed dynamic import once before
 * giving up (#1211).  Covers the stale-deployment scenario where the browser
 * has a cached `index.html` pointing to JS chunks that the server has already
 * purged.
 */
export function lazyRetry<T extends ComponentType<unknown>>(
  factory: () => Promise<{ default: T }>,
): React.LazyExoticComponent<T> {
  return lazy(() =>
    factory().catch(() =>
      // One retry — a second failure propagates to the ErrorBoundary.
      factory(),
    ),
  );
}
