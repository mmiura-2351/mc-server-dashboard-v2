import { expect, test } from "@playwright/test";
import { uniqueUser } from "./api.ts";
import { signIn } from "./ui.ts";

// register -> login -> reload keeps session -> logout, all through the UI
// (WEBUI_SPEC.md 6.1). A fresh user per run keeps the flow independent and
// idempotent.
test("register, login, reload keeps the session, logout", async ({ page }) => {
  const user = uniqueUser("auth");

  // Register through the form; on success the app routes to /login.
  await page.goto("/register");
  await page.getByLabel("Username").fill(user.username);
  await page.getByLabel("Email").fill(user.email);
  // Two password fields (password + confirm) share the "Password" prefix; fill
  // by exact id to keep them apart.
  await page.locator("#register-password").fill(user.password);
  await page.locator("#register-confirm").fill(user.password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/login/);

  // Log in; the app leaves /login for the authenticated shell.
  await signIn(page, user.username, user.password);

  // Reload: the httpOnly refresh cookie re-bootstraps the session, so we stay
  // signed in (not bounced to /login).
  await page.reload();
  await expect(page).not.toHaveURL(/\/login/);

  // Log out from the account page; the session resets and routes to /login.
  await page.goto("/account");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);

  // Session is gone: a protected route bounces back to /login.
  await page.goto("/account");
  await expect(page).toHaveURL(/\/login/);
});
