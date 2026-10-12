import { expect, test } from "@playwright/test";
import {
  adminCredentials,
  registerUser,
  uniqueName,
  uniqueUser,
} from "./api.ts";
import { signIn, signOut } from "./ui.ts";

test("admin provisions a community an owner can see", async ({
  page,
  request,
}) => {
  // Register the owner before opening the dialog so the owner picker can list them.
  const owner = uniqueUser("owner");
  const { id: ownerId } = await registerUser(request, owner);
  const communityName = uniqueName("Community");

  await signIn(page, adminCredentials.username, adminCredentials.password);
  await page.goto("/admin/communities");

  await page
    .getByRole("button", { name: "Provision community", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "Provision community" });
  await dialog.getByLabel("Community name").fill(communityName);
  await dialog.getByLabel("Initial owner").selectOption(ownerId);
  await dialog.getByRole("button", { name: "Provision", exact: true }).click();

  await expect(dialog).toBeHidden();
  await expect(
    page.getByRole("row", { name: new RegExp(communityName) }),
  ).toBeVisible();

  // Community listings are membership-scoped; verify visibility as the new owner.
  await signOut(page);
  await signIn(page, owner.username, owner.password);
  await expect(
    page.getByRole("combobox", { name: "Switch community" }),
  ).toContainText(communityName);
});
