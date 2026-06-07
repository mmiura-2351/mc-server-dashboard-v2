/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Local API the dev server proxies to. The browser only ever talks to the dev
// server, so the UI stays same-origin with the API and no CORS is added
// anywhere (WEBUI_SPEC.md 7.7). Override with VITE_API_PROXY_TARGET to point at
// an API on another host/port; the default matches the API's http_port.
const API_TARGET = process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    // The entire HTTP API (REST + WebSocket) lives under /api (issue #498), so a
    // single prefix is forwarded to the local API and every other path falls
    // through to the SPA for client-side routing. /api can never be an SPA
    // route, so the Accept-header bypass the old per-prefix proxy needed for
    // colliding deep-links is gone. ws:true upgrades the event streams.
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    // Unit tests live under src/; the Playwright E2E specs under e2e/ are run by
    // Playwright (npm run e2e), not Vitest — scope the include so Vitest does
    // not try to load e2e/*.spec.ts (which import @playwright/test).
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
});
