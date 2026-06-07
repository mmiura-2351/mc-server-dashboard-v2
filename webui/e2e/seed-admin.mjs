// Seed the very first platform admin for the E2E run.
//
// There is no seeded admin (DEPLOYMENT.md Section 5): register the first user
// over HTTP, then promote it to platform admin directly in the database — the
// one out-of-band bootstrap step. This mirrors what the live deploy does by
// hand, scripted so the suite always starts from a known admin.
//
// Idempotent: a re-run against an already-seeded database treats the register
// 409 as "already there" and re-asserts the platform-admin flag, so repeated
// local runs against the same database stay green.
//
// Inputs (env): MCD_E2E_API_URL (default http://127.0.0.1:8000),
// MCD_E2E_ADMIN_USERNAME / MCD_E2E_ADMIN_PASSWORD / MCD_E2E_ADMIN_EMAIL, and
// MCD_E2E_PROMOTE_CMD — the shell command that flips is_platform_admin for the
// admin username (the DB access the API itself cannot grant for the first
// admin). The harness wires this to a `uv run` one-liner against Postgres.

import { execSync } from "node:child_process";

const API_URL = process.env.MCD_E2E_API_URL ?? "http://127.0.0.1:8000";
const USERNAME = process.env.MCD_E2E_ADMIN_USERNAME ?? "e2e-admin";
const PASSWORD = process.env.MCD_E2E_ADMIN_PASSWORD ?? "E2eAdminPass!234";
const EMAIL = process.env.MCD_E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";
const PROMOTE_CMD = process.env.MCD_E2E_PROMOTE_CMD;

async function main() {
  const res = await fetch(`${API_URL}/api/users`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      username: USERNAME,
      email: EMAIL,
      password: PASSWORD,
    }),
  });
  if (res.status !== 201 && res.status !== 409) {
    throw new Error(`admin register failed: ${res.status} ${await res.text()}`);
  }

  if (PROMOTE_CMD === undefined || PROMOTE_CMD === "") {
    throw new Error("MCD_E2E_PROMOTE_CMD is required to promote the admin");
  }
  execSync(PROMOTE_CMD, { stdio: "inherit", env: process.env });

  // Confirm the admin can authenticate and is platform admin, so a botched
  // promotion fails the setup loudly rather than the first admin-only test.
  const login = await fetch(`${API_URL}/api/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username: USERNAME, password: PASSWORD }),
  });
  if (!login.ok) {
    throw new Error(
      `admin login failed: ${login.status} ${await login.text()}`,
    );
  }
  const { access_token } = await login.json();
  const me = await fetch(`${API_URL}/api/users/me`, {
    headers: { authorization: `Bearer ${access_token}` },
  });
  const body = await me.json();
  if (body.is_platform_admin !== true) {
    throw new Error("admin was registered but not promoted to platform admin");
  }
  console.log(`seeded platform admin: ${USERNAME}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
