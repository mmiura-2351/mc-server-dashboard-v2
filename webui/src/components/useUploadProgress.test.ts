import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useUploadProgress } from "./useUploadProgress.ts";

describe("useUploadProgress", () => {
  beforeEach(() => {
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
});
