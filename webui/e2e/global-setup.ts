// The caller boots the API and database; fail early if /healthz reports ok=false.
// A 200 alone is insufficient because degraded health still returns 200.

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
