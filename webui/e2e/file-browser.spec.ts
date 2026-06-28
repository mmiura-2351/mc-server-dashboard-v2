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

// The file browser requires a worker-assigned server — skip in environments
// that only run the API (the default CI setup). Set MCD_E2E_HAS_WORKER=1 to
// enable.
const HAS_WORKER = process.env.MCD_E2E_HAS_WORKER === "1";

// Helper: create a user, community, and server for file-browser tests. Returns
// the IDs and credentials needed to drive the UI and clean up via API.
async function setupServer(
  request: import("@playwright/test").APIRequestContext,
) {
  const owner = uniqueUser("filebrowser");
  const { id: ownerId } = await registerUser(request, owner);
  const { id: communityId } = await provisionCommunity(
    request,
    ownerId,
    uniqueName("FileBrowser"),
  );
  const token = await login(request, owner.username, owner.password);

  // Create a stopped Vanilla server via API (avoids the wizard flow and its
  // external catalog dependency).
  const res = await request.post(
    `${API_URL}/api/communities/${communityId}/servers`,
    {
      headers: { authorization: `Bearer ${token}` },
      data: {
        name: uniqueName("dnd-test").toLowerCase(),
        server_type: "vanilla",
        mc_edition: "java",
        mc_version: "1.21.6",
        execution_backend: "container",
        game_port: 25565,
        eula_accepted: true,
      },
    },
  );
  expect(res.status(), await res.text()).toBe(201);
  const server = await res.json();

  return { owner, communityId, serverId: server.id, token };
}

// Helper: open the file browser tab for a given server.
async function openFilesBrowser(
  page: import("@playwright/test").Page,
  communityId: string,
  serverId: string,
) {
  await page.goto(`/communities/${communityId}/servers/${serverId}`);
  await page.getByRole("tab", { name: /Files/ }).click();
  // Wait for the file tree container to appear (may show loading, empty, or
  // a listing depending on worker state).
  await expect(page.locator(".file-tree")).toBeVisible();
}

// Helper: delete a file via the API.
async function deleteFile(
  request: import("@playwright/test").APIRequestContext,
  communityId: string,
  serverId: string,
  token: string,
  path: string,
) {
  await request.delete(
    `${API_URL}/api/communities/${communityId}/servers/${serverId}/files?path=${encodeURIComponent(path)}`,
    { headers: { authorization: `Bearer ${token}` } },
  );
}

test.describe("file browser drag-and-drop upload", () => {
  test.skip(
    !HAS_WORKER,
    "requires MCD_E2E_HAS_WORKER=1 (worker-assigned server)",
  );

  test("drag-and-drop file upload works", async ({ page, request }) => {
    const { owner, communityId, serverId, token } = await setupServer(request);
    await signIn(page, owner.username, owner.password);
    await openFilesBrowser(page, communityId, serverId);

    // Dispatch a DragEvent in the browser context where DataTransfer is real.
    await page.evaluate(() => {
      const content = new Uint8Array([104, 101, 108, 108, 111]); // "hello"
      const file = new File([content], "e2e-dnd-test.txt", {
        type: "text/plain",
      });
      const dt = new DataTransfer();
      dt.items.add(file);

      const dropZone = document.querySelector(".file-tree");
      if (!dropZone) throw new Error("drop zone not found");

      dropZone.dispatchEvent(
        new DragEvent("dragenter", { bubbles: true, dataTransfer: dt }),
      );
      dropZone.dispatchEvent(
        new DragEvent("drop", { bubbles: true, dataTransfer: dt }),
      );
    });

    // Wait for the uploaded file to appear in the listing.
    await expect(page.locator("text=e2e-dnd-test.txt")).toBeVisible({
      timeout: 15_000,
    });

    // Clean up: delete the uploaded file.
    await deleteFile(request, communityId, serverId, token, "e2e-dnd-test.txt");
  });

  test("drag-and-drop ZIP file upload works", async ({ page, request }) => {
    const { owner, communityId, serverId, token } = await setupServer(request);
    await signIn(page, owner.username, owner.password);
    await openFilesBrowser(page, communityId, serverId);

    await page.evaluate(() => {
      // Minimal ZIP file bytes (empty archive).
      const zipBytes = new Uint8Array([
        0x50, 0x4b, 0x05, 0x06, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0,
      ]);
      const file = new File([zipBytes], "e2e-dnd-test.zip", {
        type: "application/zip",
      });
      const dt = new DataTransfer();
      dt.items.add(file);

      const dropZone = document.querySelector(".file-tree");
      if (!dropZone) throw new Error("drop zone not found");

      dropZone.dispatchEvent(
        new DragEvent("dragenter", { bubbles: true, dataTransfer: dt }),
      );
      dropZone.dispatchEvent(
        new DragEvent("drop", { bubbles: true, dataTransfer: dt }),
      );
    });

    await expect(page.locator("text=e2e-dnd-test.zip")).toBeVisible({
      timeout: 15_000,
    });

    await deleteFile(request, communityId, serverId, token, "e2e-dnd-test.zip");
  });
});
