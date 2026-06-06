/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Local API the dev server proxies to. The browser only ever talks to the dev
// server, so the UI stays same-origin with the API and no CORS is added
// anywhere (WEBUI_SPEC.md 7.7). Override with VITE_API_PROXY_TARGET to point at
// an API on another host/port; the default matches the API's http_port.
const API_TARGET = process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000";

// Top-level API path prefixes (the API's router roots, WEBUI_SPEC.md Section 2).
// Every request under these is forwarded to the local API; every other path
// falls through to the SPA so client-side routing keeps working in dev.
const API_PREFIXES = [
  "/auth",
  "/users",
  "/admin",
  "/communities",
  "/workers",
  "/versions",
  "/ports",
  "/audit",
  "/backups",
  "/healthz",
  "/readyz",
  "/metrics",
];

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      API_PREFIXES.map((prefix) => [
        prefix,
        // ws:true upgrades the WebSocket event streams under /communities
        // (WEBUI_SPEC.md 2.5) through the same proxy entry as the REST paths.
        { target: API_TARGET, changeOrigin: true, ws: true },
      ]),
    ),
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
});
