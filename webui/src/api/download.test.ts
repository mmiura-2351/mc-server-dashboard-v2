import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
import { ApiError } from "./client.ts";
import { downloadFile } from "./download.ts";

describe("downloadFile", () => {
  const clicks: HTMLAnchorElement[] = [];

  beforeEach(() => {
    clicks.length = 0;
    setAccessToken("tok-1");
    // jsdom has no object-URL plumbing; stub it.
    URL.createObjectURL = vi.fn(() => "blob:fake");
    URL.revokeObjectURL = vi.fn();
    // Capture the synthesised anchor click instead of navigating.
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicks.push(this);
    });
  });

  afterEach(() => {
    clearAccessToken();
    vi.restoreAllMocks();
  });

  it("sends the bearer token and saves the blob under the given filename", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(new Blob(["zip-bytes"]), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await downloadFile("/communities/c1/servers/s1/export", "survival.zip");

    expect(fetchMock).toHaveBeenCalledWith(
      "/communities/c1/servers/s1/export",
      expect.objectContaining({
        method: "GET",
        headers: { authorization: "Bearer tok-1" },
      }),
    );
    expect(clicks).toHaveLength(1);
    expect(clicks[0].download).toBe("survival.zip");
  });

  it("defers revoking the object URL until after the current task", async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValue(
            new Response(new Blob(["zip-bytes"]), { status: 200 }),
          ),
      );

      await downloadFile("/communities/c1/servers/s1/export", "survival.zip");

      // The revoke is scheduled, not run synchronously, so the click-initiated
      // download still has a live object URL.
      expect(URL.revokeObjectURL).not.toHaveBeenCalled();
      vi.runAllTimers();
      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:fake");
    } finally {
      vi.useRealTimers();
    }
  });

  it("throws a typed ApiError carrying the reason on a 409", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ reason: "server_unsettled" }), {
          status: 409,
          headers: { "content-type": "application/problem+json" },
        }),
      ),
    );

    await expect(
      downloadFile("/communities/c1/servers/s1/export", "x.zip"),
    ).rejects.toMatchObject({ status: 409, reason: "server_unsettled" });
    await expect(
      downloadFile("/communities/c1/servers/s1/export", "x.zip"),
    ).rejects.toBeInstanceOf(ApiError);
    expect(clicks).toHaveLength(0);
  });
});
