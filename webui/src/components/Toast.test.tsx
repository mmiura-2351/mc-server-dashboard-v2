import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider, type ToastVariant, useToast } from "./Toast.tsx";

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

// Captures showToast so a test can raise toasts of any message and variant.
let raise: (message: string, variant: ToastVariant) => void;
function Capture() {
  raise = useToast().showToast;
  return null;
}

function renderToasts() {
  render(
    <ToastProvider>
      <Capture />
    </ToastProvider>,
  );
}

function show(message: string, variant: ToastVariant) {
  act(() => {
    raise(message, variant);
  });
}

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
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

    const toast = screen.getByRole("alert");
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

  it("clears the auto-dismiss timer when a toast is dismissed early", () => {
    render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "ok" }));
    expect(vi.getTimerCount()).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });

  // Success toasts stay 2 s plus 100 ms per character, clamped to 3.2-12 s.
  it.each([
    ["a short message for the 3.2 s floor", "Saved", 3200],
    ["a medium message for a length-scaled time", "x".repeat(50), 7000],
    ["a long message for the 12 s ceiling", "x".repeat(300), 12000],
    // 28 characters with no word breaks: counted per character, not per word.
    ["Japanese text per character", "保存しました。".repeat(4), 4800],
  ])("keeps %s", (_, message, durationMs) => {
    renderToasts();
    show(message, "success");

    advance(durationMs - 1);
    expect(screen.getByText(message)).toBeInTheDocument();

    advance(1);
    expect(screen.queryByText(message)).not.toBeInTheDocument();
  });

  it("keeps an error toast until it is dismissed", () => {
    renderToasts();
    show("Failed", "error");

    advance(60_000);
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });

  it("announces an error toast assertively as an alert", () => {
    renderToasts();
    show("Failed", "error");

    const toast = screen.getByRole("alert");
    expect(toast).toHaveTextContent("Failed");
    expect(toast).toHaveAttribute("aria-live", "assertive");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("announces a success toast politely as a status", () => {
    renderToasts();
    show("Saved", "success");

    const toast = screen.getByRole("status");
    expect(toast).toHaveTextContent("Saved");
    expect(toast).toHaveAttribute("aria-live", "polite");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("pauses while hovered and resumes with the time left", () => {
    renderToasts();
    show("x".repeat(50), "success"); // 7 s

    advance(3000);
    fireEvent.mouseEnter(screen.getByRole("status"));
    advance(60_000);
    expect(screen.getByRole("status")).toBeInTheDocument();

    fireEvent.mouseLeave(screen.getByRole("status"));
    advance(3999);
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("pauses while focused and resumes with the time left", () => {
    renderToasts();
    show("x".repeat(50), "success"); // 7 s
    const close = screen.getByRole("button", { name: "Close" });

    advance(3000);
    act(() => close.focus());
    advance(60_000);
    expect(screen.getByRole("status")).toBeInTheDocument();

    act(() => close.blur());
    advance(3999);
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("stays paused while focused after the pointer leaves", () => {
    renderToasts();
    show("x".repeat(50), "success"); // 7 s
    const close = screen.getByRole("button", { name: "Close" });

    advance(3000);
    fireEvent.mouseEnter(screen.getByRole("status"));
    act(() => close.focus());
    fireEvent.mouseLeave(screen.getByRole("status"));
    advance(60_000);
    expect(screen.getByRole("status")).toBeInTheDocument();

    act(() => close.blur());
    advance(3999);
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("dismisses only the focused toast on Escape", () => {
    renderToasts();
    show("First", "error");
    show("Second", "error");
    const [first] = screen.getAllByRole("button", { name: "Close" });

    act(() => first.focus());
    fireEvent.keyDown(first, { key: "Escape" });

    expect(screen.queryByText("First")).not.toBeInTheDocument();
    expect(screen.getByText("Second")).toBeInTheDocument();
  });

  it("keeps the Escape that dismisses a toast from the page underneath", () => {
    // Modal and Popover close on an Escape that reaches the document.
    const onDocumentKeyDown = vi.fn();
    document.addEventListener("keydown", onDocumentKeyDown);
    try {
      renderToasts();
      show("Failed", "error");
      const close = screen.getByRole("button", { name: "Close" });

      act(() => close.focus());
      fireEvent.keyDown(close, { key: "Escape" });

      expect(screen.queryByText("Failed")).not.toBeInTheDocument();
      expect(onDocumentKeyDown).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener("keydown", onDocumentKeyDown);
    }
  });

  it("shows at most five toasts, dropping the oldest", () => {
    renderToasts();
    for (const n of [1, 2, 3, 4, 5, 6]) show(`Saved ${n}`, "success");

    expect(screen.getAllByRole("status")).toHaveLength(5);
    expect(screen.queryByText("Saved 1")).not.toBeInTheDocument();
    expect(screen.getByText("Saved 6")).toBeInTheDocument();
  });

  it("drops the oldest timed toast before any error", () => {
    renderToasts();
    show("Failed 1", "error");
    show("Saved 1", "success");
    show("Failed 2", "error");
    show("Saved 2", "success");
    show("Failed 3", "error");

    show("Failed 4", "error");

    expect(screen.queryByText("Saved 1")).not.toBeInTheDocument();
    for (const text of ["Failed 1", "Failed 2", "Saved 2", "Failed 3"]) {
      expect(screen.getByText(text)).toBeInTheDocument();
    }
    expect(screen.getByText("Failed 4")).toBeInTheDocument();
  });

  it("drops the oldest error once only errors are showing", () => {
    renderToasts();
    for (const n of [1, 2, 3, 4, 5]) show(`Failed ${n}`, "error");

    show("Saved", "success");

    expect(screen.queryByText("Failed 1")).not.toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(4);
    expect(screen.getByText("Saved")).toBeInTheDocument();
  });

  it("collapses a repeated error into the one showing, with a count", () => {
    renderToasts();
    show("Failed", "error");
    show("Failed", "error");
    show("Failed", "error");

    const [toast] = screen.getAllByRole("alert");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(toast).toHaveTextContent("Failed");
    expect(toast).toHaveTextContent("×3");
  });

  it("counts a repeated error as the newest when dropping", () => {
    renderToasts();
    for (const n of [1, 2, 3, 4, 5]) show(`Failed ${n}`, "error");
    show("Failed 1", "error");

    show("Failed 6", "error");

    expect(screen.getByText("Failed 1")).toBeInTheDocument();
    expect(screen.queryByText("Failed 2")).not.toBeInTheDocument();
  });

  it("does not collapse a repeated timed toast", () => {
    renderToasts();
    show("Saved", "success");
    show("Saved", "success");

    expect(screen.getAllByRole("status")).toHaveLength(2);
  });
});
