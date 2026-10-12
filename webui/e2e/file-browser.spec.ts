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

// File-browser flows need a Worker; API-only CI skips them unless MCD_E2E_HAS_WORKER=1.
const HAS_WORKER = process.env.MCD_E2E_HAS_WORKER === "1";

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

  // Create via the API to avoid the wizard's external version-catalog dependency.
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
        // Omit game_port so the API allocates a free port for each fixture.
        eula_accepted: true,
      },
    },
  );
  expect(res.status(), await res.text()).toBe(201);
  const server = await res.json();

  return { owner, communityId, serverId: server.id, token };
}

async function openFilesBrowser(
  page: import("@playwright/test").Page,
  communityId: string,
  serverId: string,
) {
  await page.goto(`/communities/${communityId}/servers/${serverId}`);
  await page.getByRole("tab", { name: /Files/ }).click();
  await expect(page.locator(".file-tree")).toBeVisible();
}

async function uploadFile(
  request: import("@playwright/test").APIRequestContext,
  communityId: string,
  serverId: string,
  token: string,
  fileName: string,
  content: string,
) {
  const form = new FormData();
  form.append("file", new Blob([content], { type: "text/plain" }), fileName);
  const res = await request.post(
    `${API_URL}/api/communities/${communityId}/servers/${serverId}/files/upload?path=&extract=false`,
    {
      headers: { authorization: `Bearer ${token}` },
      multipart: {
        file: {
          name: fileName,
          mimeType: "text/plain",
          buffer: Buffer.from(content),
        },
      },
    },
  );
  expect(res.ok(), await res.text()).toBeTruthy();
}

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

// Playwright cannot drag files from outside the browser; dispatch a synthetic drop instead.
async function dropFileOnTree(
  page: import("@playwright/test").Page,
  fileName: string,
  content: string,
) {
  await page.evaluate(
    ({ fileName, content }) => {
      const file = new File([content], fileName, { type: "text/plain" });
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
    },
    { fileName, content },
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

    await dropFileOnTree(page, "e2e-dnd-test.txt", "hello");

    await expect(page.locator("text=e2e-dnd-test.txt")).toBeVisible({
      timeout: 15_000,
    });

    await deleteFile(request, communityId, serverId, token, "e2e-dnd-test.txt");
  });

  test("drag-and-drop ZIP file upload works", async ({ page, request }) => {
    const { owner, communityId, serverId, token } = await setupServer(request);
    await signIn(page, owner.username, owner.password);
    await openFilesBrowser(page, communityId, serverId);

    await page.evaluate(() => {
      // An empty ZIP archive's end-of-central-directory record.
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

test.describe("file browser drag-and-drop overwrite", () => {
  test.skip(
    !HAS_WORKER,
    "requires MCD_E2E_HAS_WORKER=1 (worker-assigned server)",
  );

  test("overwrites the file when user confirms", async ({ page, request }) => {
    const { owner, communityId, serverId, token } = await setupServer(request);
    await signIn(page, owner.username, owner.password);

    await uploadFile(
      request,
      communityId,
      serverId,
      token,
      "ow-confirm.txt",
      "original",
    );

    await openFilesBrowser(page, communityId, serverId);
    await expect(page.locator("text=ow-confirm.txt")).toBeVisible({
      timeout: 15_000,
    });

    await dropFileOnTree(page, "ow-confirm.txt", "updated");

    await expect(page.locator(".modal")).toBeVisible({ timeout: 10_000 });
    await page
      .locator(".modal button")
      .filter({ hasText: /Overwrite|上書き/ })
      .click();

    await expect(page.locator(".modal")).not.toBeVisible({ timeout: 5_000 });
    await expect(page.locator("text=ow-confirm.txt")).toBeVisible();

    await deleteFile(request, communityId, serverId, token, "ow-confirm.txt");
  });

  test("skips the file when user clicks skip", async ({ page, request }) => {
    const { owner, communityId, serverId, token } = await setupServer(request);
    await signIn(page, owner.username, owner.password);

    await uploadFile(
      request,
      communityId,
      serverId,
      token,
      "ow-skip.txt",
      "original",
    );

    await openFilesBrowser(page, communityId, serverId);
    await expect(page.locator("text=ow-skip.txt")).toBeVisible({
      timeout: 15_000,
    });

    await dropFileOnTree(page, "ow-skip.txt", "should-be-skipped");

    await expect(page.locator(".modal")).toBeVisible({ timeout: 10_000 });
    await page
      .locator(".modal button")
      .filter({ hasText: /Skip|スキップ/ })
      .click();

    await expect(page.locator(".modal")).not.toBeVisible({ timeout: 5_000 });

    await deleteFile(request, communityId, serverId, token, "ow-skip.txt");
  });
});
