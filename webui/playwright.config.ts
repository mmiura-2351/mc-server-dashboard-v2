import { createHash } from "node:crypto";
import { defineConfig, devices } from "@playwright/test";

// The caller starts the API and database; this config starts Vite for its /api proxy.
// The preview server does not apply that proxy.

const API_URL = process.env.MCD_E2E_API_URL ?? "http://127.0.0.1:8000";
const UI_HOST = "127.0.0.1";
// Derive the default port from the worktree path to reduce cross-worktree server reuse.
// Use 20000-29999 to avoid the standard service and Linux ephemeral port ranges.
const defaultUiPort =
  20000 +
  (createHash("sha256")
    .update(import.meta.dirname)
    .digest()
    .readUInt32BE(0) %
    10000);
const UI_PORT = Number(process.env.MCD_E2E_UI_PORT ?? defaultUiPort);
const UI_URL = `http://${UI_HOST}:${UI_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  // Allow time for upstream catalog requests and retry transient failures in CI.
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: UI_URL,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    // Bind to the probe's IPv4 address; localhost may resolve to IPv6 instead.
    command: `npm run dev -- --host ${UI_HOST} --port ${UI_PORT} --strictPort`,
    url: UI_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { VITE_API_PROXY_TARGET: API_URL },
  },
});
