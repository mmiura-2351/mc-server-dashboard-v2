import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { t } from "../i18n/index.ts";
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
  it("toasts the named permission on a 403 carrying the permission member", () => {
    showToast.mockReset();
    const { result } = renderHook(() => useOnForbidden(), { wrapper });

    const handled = result.current(
      new ApiError(403, { reason: "forbidden", permission: "server:start" }),
    );

    expect(handled).toBe(true);
    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining("server:start"),
      "error",
    );
  });

  it("toasts a generic message when the 403 carries no permission member", () => {
    showToast.mockReset();
    const { result } = renderHook(() => useOnForbidden(), { wrapper });

    const handled = result.current(new ApiError(403, { reason: "forbidden" }));

    expect(handled).toBe(true);
    expect(showToast).toHaveBeenCalledWith(
      expect.not.stringContaining("forbidden"),
      "error",
    );
  });

  // A denied lifecycle action names its server, as its other toasts do, and
  // stays apart from the same denial on another server (#3862).
  it("names the server the denied action was about, scoped to its id", () => {
    showToast.mockReset();
    const { result } = renderHook(() => useOnForbidden(), { wrapper });

    const handled = result.current(
      new ApiError(403, { reason: "forbidden", permission: "server:stop" }),
      { id: "s1", name: "alpha" },
    );

    expect(handled).toBe(true);
    expect(showToast).toHaveBeenCalledWith(
      `alpha: ${t("permissions.deniedNamed", { permission: "server:stop" })}`,
      "error",
      "s1",
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
