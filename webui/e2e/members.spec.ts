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

  // Use a custom role so the assignment cannot pass by re-granting Owner.
  const ownerTok = await login(request, owner.username, owner.password);
  const roleName = uniqueName("Crew");
  await createRole(request, ownerTok, communityId, roleName);

  await signIn(page, owner.username, owner.password);
  await page.goto(`/communities/${communityId}/settings`);

  await page.getByRole("button", { name: "Add member…" }).click();
  await page.getByLabel("Username").fill(member.username);
  await page.getByRole("button", { name: "Add member", exact: true }).click();

  const row = page.getByRole("row", { name: new RegExp(member.username) });
  await expect(row).toBeVisible();

  await row.getByRole("button", { name: "Assign role" }).click();
  await page.getByRole("menuitem", { name: roleName }).click();

  await expect(row.getByText(roleName)).toBeVisible();

  const members = await listMembers(request, ownerTok, communityId);
  const added = members.find((m) => m.username === member.username);
  expect(added, "member should exist").toBeTruthy();
  expect(added?.role_names).toContain(roleName);
});
