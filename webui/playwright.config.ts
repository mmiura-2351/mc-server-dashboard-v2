import { defineConfig, devices } from "@playwright/test";

// Playwright E2E config (issue #491). The suite drives the real UI against a
// real API + Postgres over the critical flows.
//
// Division of labour:
//   - The API (uvicorn + Postgres + migrations + a seeded admin) is booted by
//     the orchestration around this config — `make webui-e2e` locally, the e2e
//     workflow in CI — because it needs the database up first. globalSetup only
//     asserts the API is reachable, so a misconfigured run fails fast with a
//     clear message instead of a wall of UI timeouts.
//   - The UI is the Vite dev server (`npm run dev`), started here as a
//     webServer. Dev is the honest cheap option: the dev-server proxy
//     (vite.config.ts) forwards the /api prefix (REST + the WebSocket event
//     streams) to the API, keeping the browser same-origin with the API exactly
//     as production does — `vite preview` does not apply that proxy. The proxy
//     target follows MCD_E2E_API_URL via VITE_API_PROXY_TARGET.
//
// Chromium only for the first cut.

const API_URL = process.env.MCD_E2E_API_URL ?? "http://127.0.0.1:8000";
const UI_HOST = "127.0.0.1";
const UI_PORT = Number(process.env.MCD_E2E_UI_PORT ?? 5173);
const UI_URL = `http://${UI_HOST}:${UI_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  // E2E is the slow path: keep it serial-ish and give the network-backed
  // version catalog room. CI retries once to absorb a transient source blip.
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
    // Bind the dev server to the same literal host Playwright probes
    // (UI_URL). Vite defaults to "localhost", which on the CI runner can
    // resolve to IPv6 ::1 (Node >=17 keeps the resolver's verbatim order)
    // while `url` is IPv4 127.0.0.1 — the probe then never connects and the
    // run dies with a 120s "config.webServer" timeout. Pinning --host keeps
    // the listen address and the probe address identical.
    command: `npm run dev -- --host ${UI_HOST} --port ${UI_PORT} --strictPort`,
    url: UI_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { VITE_API_PROXY_TARGET: API_URL },
  },
});
