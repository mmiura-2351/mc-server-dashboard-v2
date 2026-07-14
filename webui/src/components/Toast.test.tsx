import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider, useToast } from "./Toast.tsx";

function Harness() {
  const { showToast } = useToast();
  return (
    <div>
      <button type="button" onClick={() => showToast("Saved", "success")}>
        ok
      </button>
      <button type="button" onClick={() => showToast("Failed", "error")}>
        err
      </button>
    </div>
  );
}

describe("Toast", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("shows a toast with its message and variant when triggered", () => {
    render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "err" }));

    const toast = screen.getByRole("status");
    expect(toast).toHaveTextContent("Failed");
    expect(toast).toHaveClass("toast", "error");
  });

  it("auto-dismisses the toast after the timeout", () => {
    render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "ok" }));
    expect(screen.getByRole("status")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(4000);
    });

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("clears the pending auto-dismiss timer on unmount", () => {
    const { unmount } = render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "ok" }));
    expect(vi.getTimerCount()).toBe(1);

    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });
});
