import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useNavHistory } from "./useNavHistory.ts";

describe("useNavHistory", () => {
  it("starts at root with no open file", () => {
    const { result } = renderHook(() => useNavHistory());
    expect(result.current.current).toEqual({ dir: "", openFile: null });
    expect(result.current.canGoBack).toBe(false);
    expect(result.current.canGoForward).toBe(false);
  });

  it("navigate pushes a new state", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.navigate({ dir: "world", openFile: null }));
    expect(result.current.current).toEqual({ dir: "world", openFile: null });
    expect(result.current.canGoBack).toBe(true);
    expect(result.current.canGoForward).toBe(false);
  });

  it("goBack returns to the previous state", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.navigate({ dir: "world", openFile: null }));
    act(() => result.current.goBack());
    expect(result.current.current).toEqual({ dir: "", openFile: null });
    expect(result.current.canGoBack).toBe(false);
    expect(result.current.canGoForward).toBe(true);
  });

  it("goForward returns to the next state after goBack", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.navigate({ dir: "world", openFile: null }));
    act(() => result.current.goBack());
    act(() => result.current.goForward());
    expect(result.current.current).toEqual({ dir: "world", openFile: null });
    expect(result.current.canGoBack).toBe(true);
    expect(result.current.canGoForward).toBe(false);
  });

  it("navigate after goBack clears the forward stack", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.navigate({ dir: "world", openFile: null }));
    act(() => result.current.navigate({ dir: "world/nether", openFile: null }));
    act(() => result.current.goBack());
    act(() => result.current.navigate({ dir: "config", openFile: null }));
    expect(result.current.canGoForward).toBe(false);
    expect(result.current.current).toEqual({ dir: "config", openFile: null });
    expect(result.current.canGoBack).toBe(true);
  });

  it("tracks file opens in history", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() =>
      result.current.navigate({ dir: "", openFile: "server.properties" }),
    );
    expect(result.current.current).toEqual({
      dir: "",
      openFile: "server.properties",
    });
    act(() => result.current.goBack());
    expect(result.current.current).toEqual({ dir: "", openFile: null });
  });

  it("accepts an initial state", () => {
    const { result } = renderHook(() =>
      useNavHistory({ dir: "world", openFile: null }),
    );
    expect(result.current.current).toEqual({ dir: "world", openFile: null });
    expect(result.current.canGoBack).toBe(false);
    expect(result.current.canGoForward).toBe(false);
  });

  it("goBack is a no-op at the start", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.goBack());
    expect(result.current.current).toEqual({ dir: "", openFile: null });
  });

  it("goForward is a no-op at the end", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.navigate({ dir: "world", openFile: null }));
    act(() => result.current.goForward());
    expect(result.current.current).toEqual({ dir: "world", openFile: null });
  });

  it("does not push duplicate when navigating to current state", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.navigate({ dir: "a", openFile: null }));
    expect(result.current.canGoBack).toBe(true);

    act(() => result.current.navigate({ dir: "a", openFile: null }));
    act(() => result.current.goBack());
    expect(result.current.current).toEqual({ dir: "", openFile: null });
    expect(result.current.canGoBack).toBe(false);
  });

  it("jumpTo replaces the current entry without truncating forward history", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.navigate({ dir: "a", openFile: null }));
    act(() => result.current.navigate({ dir: "b", openFile: null }));
    act(() => result.current.jumpTo({ dir: "x", openFile: null }));
    expect(result.current.current).toEqual({ dir: "x", openFile: null });
    expect(result.current.canGoForward).toBe(false);
    act(() => result.current.goBack());
    expect(result.current.current).toEqual({ dir: "a", openFile: null });
    act(() => result.current.goBack());
    expect(result.current.current).toEqual({ dir: "", openFile: null });
    expect(result.current.canGoBack).toBe(false);
  });

  it("jumpTo finds a matching entry and preserves forward history", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.navigate({ dir: "a", openFile: null }));
    act(() => result.current.navigate({ dir: "b", openFile: null }));
    act(() => result.current.jumpTo({ dir: "a", openFile: null }));
    expect(result.current.current).toEqual({ dir: "a", openFile: null });
    expect(result.current.canGoForward).toBe(true);
    act(() => result.current.goForward());
    expect(result.current.current).toEqual({ dir: "b", openFile: null });
  });

  it("jumpTo is a no-op when target matches current state", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.navigate({ dir: "a", openFile: null }));
    act(() => result.current.jumpTo({ dir: "a", openFile: null }));
    act(() => result.current.goBack());
    expect(result.current.current).toEqual({ dir: "", openFile: null });
    expect(result.current.canGoBack).toBe(false);
  });

  it("handles a longer navigation chain", () => {
    const { result } = renderHook(() => useNavHistory());
    act(() => result.current.navigate({ dir: "a", openFile: null }));
    act(() => result.current.navigate({ dir: "a/b", openFile: null }));
    act(() => result.current.navigate({ dir: "a/b", openFile: "a/b/x.txt" }));

    act(() => result.current.goBack());
    act(() => result.current.goBack());
    expect(result.current.current).toEqual({ dir: "a", openFile: null });

    act(() => result.current.goForward());
    expect(result.current.current).toEqual({ dir: "a/b", openFile: null });
  });
});
