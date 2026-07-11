/**
 * Shared render helper mirroring the ambient provider stack from `main.tsx`
 * (#423). Tests that mount the real `<App/>` under its providers should use this
 * instead of hand-rolling the stack, so that adding a provider to the app is a
 * one-place change here rather than an edit fanned out across every test.
 *
 * Test-friendly differences from `main.tsx`: a fresh `QueryClient` per call with
 * retries off (so error cases fail fast and state never leaks between cases),
 * and `MemoryRouter` in place of `BrowserRouter` so a test can drive the route
 * via `initialEntries`. Network mocks stay file-local — this helper owns only
 * the provider composition.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { App } from "../App.tsx";
import { SessionProvider } from "../auth/SessionProvider.tsx";
import { ActiveCommunityProvider } from "../permissions/ActiveCommunityProvider.tsx";

function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

interface RenderAppOptions {
  /** Initial router entry; mirrors `MemoryRouter`'s `initialEntries[0]`. */
  path?: string;
  /**
   * Extra nodes rendered as siblings of `<App/>` inside the provider stack,
   * e.g. a probe that reads router/session context for assertions.
   */
  extras?: ReactNode;
}

/** Render `<App/>` under the same provider stack as `main.tsx`. */
export function renderApp({ path = "/", extras }: RenderAppOptions = {}) {
  const queryClient = testQueryClient();
  const result = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <SessionProvider>
          <ActiveCommunityProvider>
            {extras}
            <App />
          </ActiveCommunityProvider>
        </SessionProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}
