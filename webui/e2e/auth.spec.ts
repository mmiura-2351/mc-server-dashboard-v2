import { expect, test } from "@playwright/test";
import { uniqueUser } from "./api.ts";
import { register, signIn } from "./ui.ts";

test("register auto-signs-in, reload keeps the session, logout, login", async ({
  page,
}) => {
  const user = uniqueUser("auth");

  await register(page, user);

  // Reload exercises session restoration from the httpOnly refresh cookie.
  await page.reload();
  await expect(page).not.toHaveURL(/\/login/);

  await page.goto("/account");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);

  await page.goto("/account");
  await expect(page).toHaveURL(/\/login/);

  await signIn(page, user.username, user.password);
});

// Bootstrap must leave the refresh cookie unchanged, even across rapid reloads.
test("rapid reloads keep the session", async ({ page }) => {
  const user = uniqueUser("auth-f5");

  await register(page, user);

  for (let i = 0; i < 5; i++) {
    await page.reload();
  }
  await expect(page).not.toHaveURL(/\/login/);

  await page.goto("/account");
  await expect(page).not.toHaveURL(/\/login/);
});

// Language changes must keep the current session alive without reloading.
test("switching the language keeps the session", async ({ page }) => {
  const user = uniqueUser("auth-lang");

  await register(page, user);
  const landed = page.url();

  await page.locator(".lang-switcher").selectOption("ja");

  await expect(page.getByRole("link", { name: "アカウント" })).toBeVisible();
  await expect(page).not.toHaveURL(/\/login/);
  expect(new URL(page.url()).pathname).toBe(new URL(landed).pathname);
});
