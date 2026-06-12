// Seed the very first platform admin for the E2E run, AND assert the
// fresh-deployment bootstrap (issue #909): the first user registered on an empty
// database is auto-granted platform admin by the API, with NO out-of-band DB
// step. This script performs exactly that bootstrap so the suite always starts
// from a known admin — and the is_platform_admin assertion below is the e2e
// coverage that the auto-grant fired.
//
// Idempotent: a re-run against an already-seeded database treats the register
// 409 as "already there" and re-asserts the platform-admin flag, so repeated
// local runs against the same database stay green.
//
// Inputs (env): MCD_E2E_API_URL (default http://127.0.0.1:8000),
// MCD_E2E_ADMIN_USERNAME / MCD_E2E_ADMIN_PASSWORD / MCD_E2E_ADMIN_EMAIL.

const API_URL = process.env.MCD_E2E_API_URL ?? "http://127.0.0.1:8000";
const USERNAME = process.env.MCD_E2E_ADMIN_USERNAME ?? "e2e-admin";
const PASSWORD = process.env.MCD_E2E_ADMIN_PASSWORD ?? "E2eAdminPass!234";
const EMAIL = process.env.MCD_E2E_ADMIN_EMAIL ?? "e2e-admin@example.com";

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

  // No promote step: on a fresh database the first registration is already a
  // platform admin (#909). Confirm the admin can authenticate and carries the
  // flag, so a broken auto-grant fails the setup loudly rather than the first
  // admin-only test.
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
    throw new Error(
      "first registered user was not auto-granted platform admin (#909)",
    );
  }
  console.log(`bootstrapped platform admin: ${USERNAME}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
