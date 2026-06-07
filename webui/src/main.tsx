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

// Re-render the app subtree when the language switches: `t()` reads a
// module-level dictionary, so a `key` keyed on the active language remounts the
// `App` subtree and every `t()` call re-evaluates — without a full page reload,
// which would tear down an in-flight session-refresh rotation and could sign
// the user out (issues #515, #512). The session/query providers sit above the
// key boundary so the switch does not re-bootstrap the session or drop cached
// queries; they carry no `t()` of their own.
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
