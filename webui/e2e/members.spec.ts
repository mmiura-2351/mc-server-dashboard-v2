import { expect, test } from "@playwright/test";
import {
  createRole,
  listMembers,
  login,
  provisionCommunity,
  registerUser,
  uniqueName,
  uniqueUser,
} from "./api.ts";
import { signIn } from "./ui.ts";

// Owner adds a member by username and assigns a role in community settings
// (WEBUI_SPEC.md 6.10). Assert the API-visible membership + role assignment.
test("owner adds a member and assigns a role", async ({ page, request }) => {
  const owner = uniqueUser("memowner");
  const member = uniqueUser("member");
  const { id: ownerId } = await registerUser(request, owner);
  await registerUser(request, member);
  const { id: communityId } = await provisionCommunity(
    request,
    ownerId,
    uniqueName("Members"),
  );

  // Seed a distinct, named role so the assign flow does not just re-grant Owner.
  const ownerTok = await login(request, owner.username, owner.password);
  const roleName = uniqueName("Crew");
  await createRole(request, ownerTok, communityId, roleName);

  await signIn(page, owner.username, owner.password);
  await page.goto(`/communities/${communityId}/settings`);

  // Add the member by exact username.
  await page.getByRole("button", { name: "Add member…" }).click();
  await page.getByLabel("Username").fill(member.username);
  await page.getByRole("button", { name: "Add member", exact: true }).click();

  // The member appears in the table.
  const row = page.getByRole("row", { name: new RegExp(member.username) });
  await expect(row).toBeVisible();

  // Assign the seeded role via the "+" picker on the member's row.
  await row.getByRole("button", { name: "Assign role" }).click();
  await page.getByRole("menuitem", { name: roleName }).click();

  // The role chip shows on the member's row.
  await expect(row.getByText(roleName)).toBeVisible();

  // API-visible outcome: the member holds the assigned role.
  const members = await listMembers(request, ownerTok, communityId);
  const added = members.find((m) => m.username === member.username);
  expect(added, "member should exist").toBeTruthy();
  expect(added?.role_names).toContain(roleName);
});
