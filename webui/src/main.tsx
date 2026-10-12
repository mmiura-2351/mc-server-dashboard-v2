import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, useSyncExternalStore } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { App } from "./App.tsx";
import { SessionProvider } from "./auth/SessionProvider.tsx";
import { getLanguage, initLanguage, subscribeLanguage } from "./i18n/index.ts";
import { ActiveCommunityProvider } from "./permissions/ActiveCommunityProvider.tsx";
import "./styles/global.css";
import "./styles/shell.css";

initLanguage();

const queryClient = new QueryClient();

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("Root element #root not found");
}

// Remount translated content on language changes without reloading.
// Keep session and query providers above the key boundary so their state survives.
function LanguageRoot() {
  const language = useSyncExternalStore(subscribeLanguage, getLanguage);
  return <App key={language} />;
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <SessionProvider>
          <ActiveCommunityProvider>
            <LanguageRoot />
          </ActiveCommunityProvider>
        </SessionProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
