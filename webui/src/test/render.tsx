/** Mirror main.tsx's providers with a fresh no-retry query client and an in-memory router per render. */

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
  path?: string;
  /** Render probes inside the provider stack as siblings of App. */
  extras?: ReactNode;
}

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
