import { expect, test } from "@playwright/test";
import {
  listServers,
  login,
  provisionCommunity,
  registerUser,
  uniqueName,
  uniqueUser,
} from "./api.ts";
import { signIn } from "./ui.ts";

// No Worker runs in CI, so verify creation rather than a running state.
// The wizard's version catalog requires network access to the upstream manifests.
test("owner creates a server via the wizard", async ({ page, request }) => {
  const owner = uniqueUser("srvowner");
  const { id: ownerId } = await registerUser(request, owner);
  const { id: communityId } = await provisionCommunity(
    request,
    ownerId,
    uniqueName("Servers"),
  );
  const serverName = uniqueName("survival").toLowerCase();

  await signIn(page, owner.username, owner.password);
  await page.goto(`/communities/${communityId}/servers/new`);

  await page.getByRole("button", { name: /Vanilla/ }).click();
  const versionSelect = page.locator("#version-select");
  await expect(versionSelect).toBeEnabled();
  await expect
    .poll(async () => (await versionSelect.inputValue()).length)
    .toBeGreaterThan(0);
  await page.getByRole("button", { name: "Next" }).click();

  await page.locator("#name-input").fill(serverName);
  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Create server", exact: true })
    .click();

  await expect(page).toHaveURL(
    new RegExp(`/communities/${communityId}/servers/[0-9a-f-]+$`),
  );
  await expect(
    page.getByRole("heading", { name: new RegExp(serverName) }),
  ).toBeVisible();

  await page.goto(`/communities/${communityId}`);
  await expect(page.getByRole("link", { name: serverName })).toBeVisible();

  const token = await login(request, owner.username, owner.password);
  const servers = await listServers(request, token, communityId);
  expect(servers.map((s) => s.name)).toContain(serverName);
});
