import { expect, test } from "@playwright/test";
import {
  login,
  provisionCommunity,
  registerUser,
  uniqueName,
  uniqueUser,
} from "./api.ts";
import { signIn } from "./ui.ts";

const API_URL = process.env.MCD_E2E_API_URL ?? "http://127.0.0.1:8000";

// Admin provisions a community for a freshly-registered owner, and the owner
// sees it in their UI.
//
// Scope note: the admin Communities page is a placeholder on this branch (the
// admin surface is not wired into App.tsx yet — see the routes in src/App.tsx),
// so provisioning cannot be driven through the admin UI. We exercise the admin
// action against the real API (the platform-admin POST /communities) and assert
// the UI-visible outcome — the owner's shell switcher lists the new community
// and lands on its dashboard.
test("admin provisions a community an owner can see", async ({
  page,
  request,
}) => {
  const owner = uniqueUser("owner");
  const { id: ownerId } = await registerUser(request, owner);
  const communityName = uniqueName("Community");

  // The admin action: provision as platform admin (POST /communities).
  await provisionCommunity(request, ownerId, communityName);

  // API-visible outcome: it now appears in the owner's communities listing
  // (GET /communities is membership-scoped, so the owner — not the admin —
  // sees it).
  const token = await login(request, owner.username, owner.password);
  const res = await request.get(`${API_URL}/communities`, {
    headers: { authorization: `Bearer ${token}` },
  });
  expect(res.ok()).toBeTruthy();
  const communities: Array<{ name: string }> = await res.json();
  expect(communities.map((c) => c.name)).toContain(communityName);

  // UI-visible outcome: the owner signs in and the shell switcher offers the
  // newly provisioned community.
  await signIn(page, owner.username, owner.password);
  await expect(
    page.getByRole("combobox", { name: "Switch community" }),
  ).toContainText(communityName);
});
