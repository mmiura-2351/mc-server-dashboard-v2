import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { App } from "./App.tsx";
import { SessionProvider } from "./auth/SessionProvider.tsx";
import { initLanguage } from "./i18n/index.ts";
import { ActiveCommunityProvider } from "./permissions/ActiveCommunityProvider.tsx";
import "./styles/global.css";
import "./styles/shell.css";

initLanguage();

const queryClient = new QueryClient();

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("Root element #root not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <SessionProvider>
          <ActiveCommunityProvider>
            <App />
          </ActiveCommunityProvider>
        </SessionProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
