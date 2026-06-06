import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { useOnForbidden } from "./useOnForbidden.ts";

const showToast = vi.fn();

vi.mock("../components/Toast.tsx", () => ({
  useToast: () => ({ showToast }),
}));

vi.mock("./ActiveCommunityProvider.tsx", () => ({
  useActiveCommunity: () => ({ communityId: "c1", setCommunityId: vi.fn() }),
}));

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      {children}
    </QueryClientProvider>
  );
}

describe("useOnForbidden", () => {
  it("toasts the named permission on a 403 with a permission-code reason", () => {
    showToast.mockReset();
    const { result } = renderHook(() => useOnForbidden(), { wrapper });

    const handled = result.current(
      new ApiError(403, { reason: "server:start" }),
    );

    expect(handled).toBe(true);
    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining("server:start"),
      "error",
    );
  });

  it("toasts a generic message when the 403 reason is not a permission code", () => {
    showToast.mockReset();
    const { result } = renderHook(() => useOnForbidden(), { wrapper });

    const handled = result.current(new ApiError(403, { reason: "forbidden" }));

    expect(handled).toBe(true);
    expect(showToast).toHaveBeenCalledWith(
      expect.not.stringContaining("forbidden"),
      "error",
    );
  });

  it("ignores non-403 errors", () => {
    showToast.mockReset();
    const { result } = renderHook(() => useOnForbidden(), { wrapper });

    expect(result.current(new ApiError(500, {}))).toBe(false);
    expect(result.current(new Error("boom"))).toBe(false);
    expect(showToast).not.toHaveBeenCalled();
  });
});
