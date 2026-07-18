import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _resetForTesting, useUploadProgress } from "./useUploadProgress.ts";

describe("useUploadProgress", () => {
  beforeEach(() => {
    _resetForTesting();
    vi.useFakeTimers();
    vi.setSystemTime(0);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts idle with no bytes tracked", () => {
    const { result } = renderHook(() => useUploadProgress());

    expect(result.current.active).toBe(false);
    expect(result.current.loaded).toBe(0);
    expect(result.current.total).toBe(0);
    expect(result.current.percent).toBe(0);
  });

  it("becomes active and records the total once started", () => {
    const { result } = renderHook(() => useUploadProgress());

    act(() => result.current.start(2000));

    expect(result.current.active).toBe(true);
    expect(result.current.total).toBe(2000);
    expect(result.current.loaded).toBe(0);
  });

  it("tracks loaded bytes and computes the percentage", () => {
    const { result } = renderHook(() => useUploadProgress());

    act(() => result.current.start(2000));
    act(() => result.current.onProgress(500, 2000));

    expect(result.current.loaded).toBe(500);
    expect(result.current.percent).toBe(25);
  });

  it("clamps the percentage to 100 when loaded exceeds total", () => {
    const { result } = renderHook(() => useUploadProgress());

    act(() => result.current.start(2000));
    act(() => result.current.onProgress(2000, 2000));

    expect(result.current.percent).toBe(100);
  });

  it("reports zero percent while the total is unknown", () => {
    const { result } = renderHook(() => useUploadProgress());

    act(() => result.current.start(0));
    act(() => result.current.onProgress(500, 0));

    expect(result.current.percent).toBe(0);
  });

  it("measures elapsed time from start", () => {
    const { result } = renderHook(() => useUploadProgress());

    act(() => result.current.start(2000));
    act(() => {
      vi.setSystemTime(3000);
      result.current.onProgress(1000, 2000);
    });

    expect(result.current.elapsedMs).toBe(3000);
  });

  it("resets back to idle", () => {
    const { result } = renderHook(() => useUploadProgress());

    act(() => result.current.start(2000));
    act(() => result.current.onProgress(1000, 2000));
    act(() => result.current.reset());

    expect(result.current.active).toBe(false);
    expect(result.current.loaded).toBe(0);
    expect(result.current.total).toBe(0);
  });

  it("returns a signal that is not aborted while active", () => {
    const { result } = renderHook(() => useUploadProgress());

    let signal!: AbortSignal;
    act(() => {
      signal = result.current.start(2000);
    });

    expect(signal.aborted).toBe(false);
  });

  it("aborts the signal and resets state when cancel is called", () => {
    const { result } = renderHook(() => useUploadProgress());

    let signal!: AbortSignal;
    act(() => {
      signal = result.current.start(2000);
    });
    act(() => result.current.cancel());

    expect(signal.aborted).toBe(true);
    expect(result.current.active).toBe(false);
  });

  it("creates a fresh signal on each start", () => {
    const { result } = renderHook(() => useUploadProgress());

    let first!: AbortSignal;
    act(() => {
      first = result.current.start(1000);
    });
    act(() => result.current.cancel());

    let second!: AbortSignal;
    act(() => {
      second = result.current.start(2000);
    });

    expect(second).not.toBe(first);
    expect(second.aborted).toBe(false);
    expect(first.aborted).toBe(true);
  });

  it("returns the signal that cancel() aborts, without a re-render", () => {
    const { result } = renderHook(() => useUploadProgress());

    let signal!: AbortSignal;
    act(() => {
      // start() returns the fresh signal synchronously — no re-render needed.
      signal = result.current.start(5000);
    });

    expect(signal.aborted).toBe(false);
    act(() => result.current.cancel());
    expect(signal.aborted).toBe(true);
  });

  it("returns a signal from start() that differs from a previous cancel'd one", () => {
    const { result } = renderHook(() => useUploadProgress());

    let first!: AbortSignal;
    act(() => {
      first = result.current.start(1000);
    });
    act(() => result.current.cancel());
    expect(first.aborted).toBe(true);

    let second!: AbortSignal;
    act(() => {
      second = result.current.start(2000);
    });
    expect(second.aborted).toBe(false);
    // A cancelled controller must not leak into the next upload.
    expect(second).not.toBe(first);
  });
});
