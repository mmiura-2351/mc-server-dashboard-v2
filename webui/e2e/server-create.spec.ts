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

// Owner creates a server through the wizard (WEBUI_SPEC.md 6.3) and the
// dashboard renders its card. No worker runs in CI, so the server parks
// unassigned/stopped — we assert creation + card rendering, not a running
// state.
//
// The version step reads the real catalog (Mojang/PaperMC/...); the API has no
// offline catalog seam, so this flow needs network to those manifests — the
// same dependency the API has in production. Vanilla is the most stable source.
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

  // Step 1: type & version. Pick Vanilla, then wait for the version <select> to
  // populate from the catalog (it preselects the latest).
  await page.getByRole("button", { name: /Vanilla/ }).click();
  const versionSelect = page.locator("#version-select");
  await expect(versionSelect).toBeEnabled();
  await expect
    .poll(async () => (await versionSelect.inputValue()).length)
    .toBeGreaterThan(0);
  await page.getByRole("button", { name: "Next" }).click();

  // Step 2: config & EULA. Name it, accept the EULA, create.
  await page.locator("#name-input").fill(serverName);
  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Create server", exact: true })
    .click();

  // The wizard routes to the new server's detail page, which shows its name.
  await expect(page).toHaveURL(
    new RegExp(`/communities/${communityId}/servers/[0-9a-f-]+$`),
  );
  await expect(
    page.getByRole("heading", { name: new RegExp(serverName) }),
  ).toBeVisible();

  // Back on the dashboard, the new server renders as a card (no worker runs in
  // CI, so it parks unassigned/stopped — we assert it renders, not a running
  // state). The card title links to the detail page.
  await page.goto(`/communities/${communityId}`);
  await expect(page.getByRole("link", { name: serverName })).toBeVisible();

  // API-visible outcome: the server exists in the community.
  const token = await login(request, owner.username, owner.password);
  const servers = await listServers(request, token, communityId);
  expect(servers.map((s) => s.name)).toContain(serverName);
});
