import { expect, test } from "@playwright/test";
import {
  createRole,
  login,
  provisionCommunity,
  registerUser,
  uniqueName,
  uniqueUser,
} from "./api.ts";
import { signIn } from "./ui.ts";

const API_URL = process.env.MCD_E2E_API_URL ?? "http://127.0.0.1:8000";

// Schedule CRUD needs no Worker, so these flows also run in API-only CI.

// Create via the API to avoid the wizard's external version-catalog dependency.
async function setupServer(
  request: import("@playwright/test").APIRequestContext,
) {
  const owner = uniqueUser("sched");
  const { id: ownerId } = await registerUser(request, owner);
  const { id: communityId } = await provisionCommunity(
    request,
    ownerId,
    uniqueName("Schedules"),
  );
  const token = await login(request, owner.username, owner.password);

  const res = await request.post(
    `${API_URL}/api/communities/${communityId}/servers`,
    {
      headers: { authorization: `Bearer ${token}` },
      data: {
        name: uniqueName("sched-test").toLowerCase(),
        server_type: "vanilla",
        mc_edition: "java",
        mc_version: "1.21.6",
        // Omit game_port so fixtures cannot collide on the global port pool.
      },
    },
  );
  expect(res.status(), await res.text()).toBe(201);
  const server = await res.json();
  return { owner, communityId, serverId: server.id, token };
}

async function listSchedules(
  request: import("@playwright/test").APIRequestContext,
  token: string,
  communityId: string,
  serverId: string,
): Promise<Array<{ id: string; name: string }>> {
  const res = await request.get(
    `${API_URL}/api/communities/${communityId}/servers/${serverId}/schedules`,
    { headers: { authorization: `Bearer ${token}` } },
  );
  expect(res.ok(), await res.text()).toBeTruthy();
  return res.json();
}

test("owner creates, toggles, and deletes a schedule", async ({
  page,
  request,
}) => {
  const { owner, communityId, serverId, token } = await setupServer(request);
  const scheduleName = uniqueName("nightly");

  await signIn(page, owner.username, owner.password);
  await page.goto(`/communities/${communityId}/servers/${serverId}`);
  await page.getByRole("tab", { name: "Schedules" }).click();

  // The timezone select includes zone names in its accessible name; use exact field labels.
  await page.getByRole("button", { name: "+ Create schedule" }).click();
  const dialog = page.getByRole("dialog", { name: "Create schedule" });
  await dialog.getByRole("textbox", { name: "Name" }).fill(scheduleName);
  await dialog.getByLabel("Action", { exact: true }).selectOption("backup");
  await dialog.getByRole("button", { name: "Create schedule" }).click();

  const row = page.getByRole("row", { name: new RegExp(scheduleName) });
  await expect(row).toBeVisible();
  await expect(row.getByText("Every 1 h")).toBeVisible();
  const toggle = row.getByRole("checkbox");
  await expect(toggle).toBeChecked();

  const created = await listSchedules(request, token, communityId, serverId);
  expect(created.map((s) => s.name)).toContain(scheduleName);

  await toggle.click();
  await expect(toggle).not.toBeChecked();

  await row.getByRole("button", { name: "Run history" }).click();
  const history = page.getByRole("dialog", { name: /Run history/ });
  await expect(history.getByText("No runs recorded yet.")).toBeVisible();
  await history.getByRole("button", { name: "Close" }).click();

  await row.getByRole("button", { name: "Delete", exact: true }).click();
  await page
    .getByRole("dialog", { name: "Delete schedule" })
    .getByRole("button", { name: "Delete schedule" })
    .click();
  await expect(page.getByText("No schedules yet.")).toBeVisible();

  const remaining = await listSchedules(request, token, communityId, serverId);
  expect(remaining.map((s) => s.name)).not.toContain(scheduleName);
});

test("schedule:read-only member sees a read-only view", async ({
  page,
  request,
}) => {
  const { communityId, serverId, token } = await setupServer(request);
  const scheduleName = uniqueName("readonly");

  const createRes = await request.post(
    `${API_URL}/api/communities/${communityId}/servers/${serverId}/schedules`,
    {
      headers: { authorization: `Bearer ${token}` },
      data: {
        name: scheduleName,
        action: "backup",
        interval_seconds: 3600,
      },
    },
  );
  expect(createRes.status(), await createRes.text()).toBe(201);

  const member = uniqueUser("schedreader");
  const { id: memberId } = await registerUser(request, member);
  const { id: roleId } = await createRole(
    request,
    token,
    communityId,
    uniqueName("Viewer"),
    ["server:read", "schedule:read"],
  );
  const addRes = await request.post(
    `${API_URL}/api/communities/${communityId}/members`,
    {
      headers: { authorization: `Bearer ${token}` },
      data: { user_id: memberId },
    },
  );
  expect(addRes.status(), await addRes.text()).toBe(201);
  const assignRes = await request.post(
    `${API_URL}/api/communities/${communityId}/members/${memberId}/roles`,
    {
      headers: { authorization: `Bearer ${token}` },
      data: { role_id: roleId },
    },
  );
  expect(assignRes.status(), await assignRes.text()).toBe(204);

  await signIn(page, member.username, member.password);
  await page.goto(`/communities/${communityId}/servers/${serverId}`);
  await page.getByRole("tab", { name: "Schedules" }).click();

  const row = page.getByRole("row", { name: new RegExp(scheduleName) });
  await expect(row).toBeVisible();
  await expect(row.getByRole("button", { name: "Run history" })).toBeVisible();
  await expect(row.getByRole("checkbox")).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "+ Create schedule" }),
  ).toHaveCount(0);
  await expect(row.getByRole("button", { name: "Edit" })).toHaveCount(0);
  await expect(
    row.getByRole("button", { name: "Delete", exact: true }),
  ).toHaveCount(0);
});
