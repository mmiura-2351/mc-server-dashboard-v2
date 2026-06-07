import { expect, test } from "@playwright/test";
import {
  adminCredentials,
  registerUser,
  uniqueName,
  uniqueUser,
} from "./api.ts";
import { signIn, signOut } from "./ui.ts";

// Admin provisions a community for a freshly-registered owner through the admin
// Communities page (AdminCommunitiesPage at /admin/communities, WEBUI_SPEC.md
// 6.12), and the owner then sees it in their UI. This drives the critical
// admin-provisioning flow end-to-end through the real admin UI (issue #501): the
// admin signs in, opens the Provision dialog, picks the owner and a name, and
// the owner's shell switcher lists the new community.
test("admin provisions a community an owner can see", async ({
  page,
  request,
}) => {
  // The owner must already exist so the Provision dialog's owner picker offers
  // them; register through the API to keep setup fast and deterministic.
  const owner = uniqueUser("owner");
  const { id: ownerId } = await registerUser(request, owner);
  const communityName = uniqueName("Community");

  // Sign in as the seeded platform admin and open the Communities page.
  await signIn(page, adminCredentials.username, adminCredentials.password);
  await page.goto("/admin/communities");

  // Open the Provision dialog, fill the name, pick the owner (the select's
  // option values are user ids), and submit.
  await page
    .getByRole("button", { name: "Provision community", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "Provision community" });
  await dialog.getByLabel("Community name").fill(communityName);
  await dialog.getByLabel("Initial owner").selectOption(ownerId);
  await dialog.getByRole("button", { name: "Provision", exact: true }).click();

  // Admin-visible outcome: the dialog closes and the new community appears in
  // the admin table.
  await expect(dialog).toBeHidden();
  await expect(
    page.getByRole("row", { name: new RegExp(communityName) }),
  ).toBeVisible();

  // Owner-visible outcome: sign the admin out, then the owner signs in and the
  // shell switcher offers the newly provisioned community (GET /communities is
  // membership-scoped, so the owner — not the admin — sees it).
  await signOut(page);
  await signIn(page, owner.username, owner.password);
  await expect(
    page.getByRole("combobox", { name: "Switch community" }),
  ).toContainText(communityName);
});
