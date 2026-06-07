// UI helpers shared across specs. These drive the real login form (rather than
// injecting a token) so the signed-in flows exercise the same path a user
// takes: the refresh cookie is set by /auth/login and the SPA bootstraps from
// it on reload.

import { expect, type Page } from "@playwright/test";

// Sign in through the login form and wait for the authenticated shell. After a
// successful login the app navigates off /login to the resolved landing.
export async function signIn(
  page: Page,
  username: string,
  password: string,
): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Username").fill(username);
  // The PasswordInput toggle button's accessible name ("Show password") also
  // contains "Password"; match the field label exactly so the lookup is
  // unambiguous (issue #535).
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login/);
}
