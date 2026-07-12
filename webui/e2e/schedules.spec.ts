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

// Schedules tab flows (WEBUI_SPEC.md 6.13, issue #1842). Schedule CRUD is
// API-side only (the runner needs no worker), so these flows run in the
// default CI setup — unlike the file-browser suite there is no worker gate.

// Helper: create a user, community, and a stopped server via API (avoids the
// wizard flow and its external catalog dependency).
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
        // game_port omitted: the API auto-allocates a free in-range port, so
        // parallel/sequential setups never collide on the global port space.
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

// Owner creates a schedule through the dialog, toggles it off, inspects run
// history, and deletes it. Asserts the API-visible outcome at each step.
test("owner creates, toggles, and deletes a schedule", async ({
  page,
  request,
}) => {
  const { owner, communityId, serverId, token } = await setupServer(request);
  const scheduleName = uniqueName("nightly");

  await signIn(page, owner.username, owner.password);
  await page.goto(`/communities/${communityId}/servers/${serverId}`);
  await page.getByRole("tab", { name: "Schedules" }).click();

  // Create via the dialog: backup action on the default 60-minute interval.
  // The timezone select's accessible name concatenates every zone option, so
  // substring label matching collides ("…San_Jua-n Ame-rica…" contains "name");
  // match roles/exact labels instead.
  await page.getByRole("button", { name: "+ Create schedule" }).click();
  const dialog = page.getByRole("dialog", { name: "Create schedule" });
  await dialog.getByRole("textbox", { name: "Name" }).fill(scheduleName);
  await dialog.getByLabel("Action", { exact: true }).selectOption("backup");
  await dialog.getByRole("button", { name: "Create schedule" }).click();

  // The row renders with the humanized cadence and an enabled toggle. The
  // default 60-minute interval (3600s) humanizes to whole hours.
  const row = page.getByRole("row", { name: new RegExp(scheduleName) });
  await expect(row).toBeVisible();
  await expect(row.getByText("Every 1 h")).toBeVisible();
  const toggle = row.getByRole("checkbox");
  await expect(toggle).toBeChecked();

  // API-visible: the schedule exists with a computed next run.
  const created = await listSchedules(request, token, communityId, serverId);
  expect(created.map((s) => s.name)).toContain(scheduleName);

  // Disable: the toggle unchecks (next_run_at nulls out server-side).
  await toggle.click();
  await expect(toggle).not.toBeChecked();

  // Run history opens (empty — the runner has not fired).
  await row.getByRole("button", { name: "Run history" }).click();
  const history = page.getByRole("dialog", { name: /Run history/ });
  await expect(history.getByText("No runs recorded yet.")).toBeVisible();
  await history.getByRole("button", { name: "Close" }).click();

  // Delete with confirm; the empty state returns.
  await row.getByRole("button", { name: "Delete", exact: true }).click();
  await page
    .getByRole("dialog", { name: "Delete schedule" })
    .getByRole("button", { name: "Delete schedule" })
    .click();
  await expect(page.getByText("No schedules yet.")).toBeVisible();

  // API-visible: the schedule is gone.
  const remaining = await listSchedules(request, token, communityId, serverId);
  expect(remaining.map((s) => s.name)).not.toContain(scheduleName);
});

// A schedule:read-only member sees the table but no write affordances.
test("schedule:read-only member sees a read-only view", async ({
  page,
  request,
}) => {
  const { owner, communityId, serverId, token } = await setupServer(request);
  const scheduleName = uniqueName("readonly");

  // Owner seeds a schedule via API.
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

  // A member holding server:read + schedule:read (no manage, no action codes).
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

  // The schedule is visible with run history, but nothing is writable.
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
