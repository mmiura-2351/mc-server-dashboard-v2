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
  build: {
    rollupOptions: {
      output: {
        // Split the React ecosystem (react, react-dom, react-router,
        // react-query) into a long-lived `vendor` chunk (#553). These libraries
        // change far less often than app code, so isolating them keeps the app
        // chunk small and lets the browser cache the vendor bundle across app
        // deploys. Route-level React.lazy in App.tsx handles the per-page
        // splitting; together they clear Vite's 500 kB initial-chunk warning.
        // The function form is used because rolldown-vite (Vite 8) only types
        // `manualChunks` as a function, not the rollup id→chunk record.
        manualChunks(id) {
          if (
            /\/node_modules\/(react|react-dom|react-router|@tanstack)\//.test(
              id,
            )
          ) {
            return "vendor";
          }
        },
      },
    },
  },
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
