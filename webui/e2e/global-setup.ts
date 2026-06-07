// Global setup: assert the real API is up before any browser starts. The API
// (uvicorn + Postgres + migrations + the seeded admin) is booted by the
// orchestration that invokes Playwright (`make webui-e2e` / the e2e workflow);
// here we just probe /healthz so a missing or degraded API fails the run with
// one clear message rather than a cascade of UI timeouts.
//
// /healthz returns 200 even when the database is degraded (ok=false), so we
// assert the body reports ok=true — the same readiness contract the api e2e
// harness uses.

const API_URL = process.env.MCD_E2E_API_URL ?? "http://127.0.0.1:8000";

export default async function globalSetup(): Promise<void> {
  const deadline = Date.now() + 60_000;
  let lastError = "";
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${API_URL}/api/healthz`);
      const body = await res.json();
      if (res.ok && body.ok === true) {
        return;
      }
      lastError = `healthz not ok: ${JSON.stringify(body)}`;
    } catch (err) {
      lastError = String(err);
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(
    `API at ${API_URL} did not become ready (database reachable): ${lastError}`,
  );
}
