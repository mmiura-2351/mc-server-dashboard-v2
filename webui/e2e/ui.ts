// Use the real login form so reloads exercise the refresh cookie set by the API.

import { expect, type Page } from "@playwright/test";
import type { TestUser } from "./api.ts";

export async function signIn(
  page: Page,
  username: string,
  password: string,
): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Username").fill(username);
  // Match the field exactly; the visibility toggle's name also contains "Password".
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

// Sign out before switching users; an authenticated session redirects away from /login.
export async function signOut(page: Page): Promise<void> {
  await page.goto("/account");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
}

// Wait for authenticated chrome; a URL other than /login already matches /register.
export async function register(page: Page, user: TestUser): Promise<void> {
  await page.goto("/register");
  await page.getByLabel("Username").fill(user.username);
  await page.getByLabel("Email").fill(user.email);
  await page.locator("#register-password").fill(user.password);
  await page.locator("#register-confirm").fill(user.password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("link", { name: "Account" })).toBeVisible();
  await expect(page).not.toHaveURL(/\/(login|register)/);
}
