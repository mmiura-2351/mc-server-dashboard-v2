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

// An F5 storm must NOT cost the session (issue #512). The bootstrap exchanges
// the refresh cookie for an access token via the NON-rotating /api/auth/session
// probe, so rapid reloads can no longer race an in-flight rotation and leave a
// revoked predecessor cookie in the jar (which, replayed past the reuse grace,
// revoked the whole token family and bounced the user to /login). Reload several
// times back-to-back and assert the user stays signed in.
test("rapid reloads keep the session", async ({ page }) => {
  const user = uniqueUser("auth-f5");

  await page.goto("/register");
  await page.getByLabel("Username").fill(user.username);
  await page.getByLabel("Email").fill(user.email);
  await page.locator("#register-password").fill(user.password);
  await page.locator("#register-confirm").fill(user.password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/login/);

  await signIn(page, user.username, user.password);

  // Five rapid reloads: every bootstrap is a non-rotating restore, so the
  // refresh cookie is never rotated and the family is never revoked.
  for (let i = 0; i < 5; i++) {
    await page.reload();
  }
  await expect(page).not.toHaveURL(/\/login/);

  // The session is genuinely intact: a fresh navigation still lands on the
  // authenticated shell rather than bouncing to /login.
  await page.goto("/account");
  await expect(page).not.toHaveURL(/\/login/);
});

// Switching the UI language must NOT cost the session (issues #515, #512). The
// switch re-renders in place rather than reloading, so it never tears down an
// in-flight refresh rotation that would leave a revoked token in the cookie jar
// and sign the user out. Assert the chrome flips to Japanese and the user stays
// on the authenticated shell.
test("switching the language keeps the session", async ({ page }) => {
  const user = uniqueUser("auth-lang");

  await page.goto("/register");
  await page.getByLabel("Username").fill(user.username);
  await page.getByLabel("Email").fill(user.email);
  await page.locator("#register-password").fill(user.password);
  await page.locator("#register-confirm").fill(user.password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/login/);

  await signIn(page, user.username, user.password);
  const landed = page.url();

  // Switch to Japanese via the top-bar switcher.
  await page.locator(".lang-switcher").selectOption("ja");

  // The account nav link renders the Japanese label, proving the live
  // re-render applied — and we are still signed in (not bounced to /login).
  await expect(page.getByRole("link", { name: "アカウント" })).toBeVisible();
  await expect(page).not.toHaveURL(/\/login/);
  expect(new URL(page.url()).pathname).toBe(new URL(landed).pathname);
});
