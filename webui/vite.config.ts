/// <reference types="vitest/config" />
import os from "node:os";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Proxy API requests through Vite so the browser stays same-origin; VITE_API_PROXY_TARGET selects the backend.
const API_TARGET = process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // Cache the React ecosystem separately so app deployments can reuse the vendor chunk.
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
    // Forward REST and WebSocket traffic under /api; other paths fall through to the SPA.
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
    // Use happy-dom for cheaper setup; tests needing jsdom-specific DOM behavior override their environment locally.
    environment: "happy-dom",
    setupFiles: ["./src/test/setup.ts"],
    // Keep Playwright specs out of Vitest's collection.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // Allow CPU contention without failing otherwise valid tests.
    testTimeout: 60_000,
    hookTimeout: 60_000,
    // Limit worker concurrency to reduce contention on shared hosts.
    maxWorkers: Math.max(1, Math.floor(os.cpus().length / 2)),
  },
});
