import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
import { ApiError, resetForTesting, setRefresher } from "./client.ts";
import {
  DownloadTooLargeError,
  downloadFile,
  fetchFileBlob,
  isAbortError,
  MAX_DOWNLOAD_BYTES,
} from "./download.ts";

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
    // Default refresher: no-op failure, so tests opt into a working refresh.
    setRefresher(() => Promise.resolve(false));
  });

  afterEach(() => {
    clearAccessToken();
    resetForTesting();
    vi.restoreAllMocks();
  });

  it("sends the bearer token and saves the blob under the given filename", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(new Blob(["zip-bytes"]), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await downloadFile("/api/communities/c1/servers/s1/export", "survival.zip");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/communities/c1/servers/s1/export",
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

      await downloadFile(
        "/api/communities/c1/servers/s1/export",
        "survival.zip",
      );

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
      downloadFile("/api/communities/c1/servers/s1/export", "x.zip"),
    ).rejects.toMatchObject({ status: 409, reason: "server_unsettled" });
    await expect(
      downloadFile("/api/communities/c1/servers/s1/export", "x.zip"),
    ).rejects.toBeInstanceOf(ApiError);
    expect(clicks).toHaveLength(0);
  });

  describe("transparent 401 refresh", () => {
    it("refreshes once and retries the download on a 401", async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(new Response(null, { status: 401 }))
        .mockResolvedValueOnce(
          new Response(new Blob(["zip-bytes"]), { status: 200 }),
        );
      vi.stubGlobal("fetch", fetchMock);
      const refresher = vi.fn(() => Promise.resolve(true));
      setRefresher(refresher);

      await downloadFile("/api/communities/c1/servers/s1/export", "out.zip");

      expect(refresher).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(clicks).toHaveLength(1);
    });

    it("does not retry when the refresh fails", async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValue(new Response(null, { status: 401 }));
      vi.stubGlobal("fetch", fetchMock);
      const refresher = vi.fn(() => Promise.resolve(false));
      setRefresher(refresher);

      const error = await downloadFile(
        "/api/communities/c1/servers/s1/export",
        "out.zip",
      ).catch((e) => e);

      expect(refresher).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(error).toBeInstanceOf(ApiError);
      expect(error.status).toBe(401);
      expect(clicks).toHaveLength(0);
    });

    it("retries only once: a second 401 after refresh surfaces the error", async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValue(new Response(null, { status: 401 }));
      vi.stubGlobal("fetch", fetchMock);
      const refresher = vi.fn(() => Promise.resolve(true));
      setRefresher(refresher);

      const error = await downloadFile(
        "/api/communities/c1/servers/s1/export",
        "out.zip",
      ).catch((e) => e);

      expect(refresher).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(error).toBeInstanceOf(ApiError);
      expect(error.status).toBe(401);
      expect(clicks).toHaveLength(0);
    });

    it("does not retry when no refresher is registered", async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValue(new Response(null, { status: 401 }));
      vi.stubGlobal("fetch", fetchMock);
      resetForTesting();

      const error = await downloadFile(
        "/api/communities/c1/servers/s1/export",
        "out.zip",
      ).catch((e) => e);

      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(error).toBeInstanceOf(ApiError);
      expect(error.status).toBe(401);
    });
  });

  describe("cancellation", () => {
    it("forwards the abort signal to fetch", async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValue(
          new Response(new Blob(["zip-bytes"]), { status: 200 }),
        );
      vi.stubGlobal("fetch", fetchMock);
      const controller = new AbortController();

      await downloadFile("/api/x", "x.zip", controller.signal);

      expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
    });

    it("rejects with the abort error and saves nothing when aborted", async () => {
      // Emulate fetch's abort contract: reject with an AbortError DOMException
      // once the signal fires.
      const fetchMock = vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise<Response>((_, reject) => {
            init.signal?.addEventListener("abort", () =>
              reject(new DOMException("aborted", "AbortError")),
            );
          }),
      );
      vi.stubGlobal("fetch", fetchMock);
      const controller = new AbortController();

      const promise = downloadFile("/api/x", "x.zip", controller.signal).catch(
        (e) => e,
      );
      controller.abort();

      const error = await promise;
      expect(isAbortError(error)).toBe(true);
      expect(clicks).toHaveLength(0);
    });
  });

  describe("isAbortError", () => {
    it("identifies the AbortError DOMException fetch rejects with", () => {
      expect(isAbortError(new DOMException("aborted", "AbortError"))).toBe(
        true,
      );
    });

    it("is false for other failures", () => {
      expect(isAbortError(new ApiError(500, undefined))).toBe(false);
      expect(isAbortError(new Error("boom"))).toBe(false);
      expect(isAbortError(undefined)).toBe(false);
    });
  });

  describe("download size cap", () => {
    it("rejects a download whose Content-Length exceeds the cap", async () => {
      const oversized = MAX_DOWNLOAD_BYTES + 1;
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(new Blob(["x"]), {
            status: 200,
            headers: { "content-length": String(oversized) },
          }),
        ),
      );

      const error = await downloadFile("/api/x", "big.zip").catch((e) => e);

      expect(error).toBeInstanceOf(DownloadTooLargeError);
      expect(error.contentLength).toBe(oversized);
      expect(clicks).toHaveLength(0);
    });

    it("allows a download exactly at the cap", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(new Blob(["ok"]), {
            status: 200,
            headers: { "content-length": String(MAX_DOWNLOAD_BYTES) },
          }),
        ),
      );

      await downloadFile("/api/x", "ok.zip");

      expect(clicks).toHaveLength(1);
    });

    it("allows a download with no Content-Length header", async () => {
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValue(
            new Response(new Blob(["chunked"]), { status: 200 }),
          ),
      );

      await downloadFile("/api/x", "chunked.zip");

      expect(clicks).toHaveLength(1);
    });

    it("exposes the size on fetchFileBlob too", async () => {
      const oversized = MAX_DOWNLOAD_BYTES + 100;
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(new Blob(["x"]), {
            status: 200,
            headers: { "content-length": String(oversized) },
          }),
        ),
      );

      const error = await fetchFileBlob("/api/x").catch((e) => e);

      expect(error).toBeInstanceOf(DownloadTooLargeError);
      expect(error.contentLength).toBe(oversized);
    });
  });
});
