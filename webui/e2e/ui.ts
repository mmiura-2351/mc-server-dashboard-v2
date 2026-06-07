// UI helpers shared across specs. These drive the real login form (rather than
// injecting a token) so the signed-in flows exercise the same path a user
// takes: the refresh cookie is set by /auth/login and the SPA bootstraps from
// it on reload.

import { expect, type Page } from "@playwright/test";
import type { TestUser } from "./api.ts";

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

// Sign out via the account page and wait for the login screen. Needed when a
// spec switches the signed-in user mid-flow: an already-authenticated session
// redirects off /login, so the next signIn never sees the form.
export async function signOut(page: Page): Promise<void> {
  await page.goto("/account");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
}

// Register through the form and wait for the authenticated shell. A successful
// registration auto-logs the user in (issue #537), so the app leaves /register
// for the resolved dashboard. Wait for the shell's Account nav link (a positive
// signed-in signal) rather than a bare URL check: the form starts on /register,
// so a "not /login" assertion would pass before the auto-login even completes
// and let a follow-up reload interrupt the in-flight login.
export async function register(page: Page, user: TestUser): Promise<void> {
  await page.goto("/register");
  await page.getByLabel("Username").fill(user.username);
  await page.getByLabel("Email").fill(user.email);
  // Two password fields (password + confirm) share the "Password" prefix, and
  // the visibility toggle's accessible name also contains "password"; fill by
  // exact id to keep them apart.
  await page.locator("#register-password").fill(user.password);
  await page.locator("#register-confirm").fill(user.password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("link", { name: "Account" })).toBeVisible();
  await expect(page).not.toHaveURL(/\/(login|register)/);
}
