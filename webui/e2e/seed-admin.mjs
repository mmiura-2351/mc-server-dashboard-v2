// The first registration on an empty database must become a platform admin.
// On reruns, accept an existing username and verify it still has admin privileges.

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
