import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { type ReactNode, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { t } from "../i18n/index.ts";
import * as focusVisible from "./focusVisible.ts";
import { Modal } from "./Modal.tsx";
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

function renderToasts(page?: ReactNode) {
  render(
    <ToastProvider>
      <Capture />
      {page}
    </ToastProvider>,
  );
}

// A button the test can take out of the page while a toast holds focus.
let removeButton = () => {};
function RemovableButton({ name }: { name: string }) {
  const [shown, setShown] = useState(true);
  removeButton = () => setShown(false);
  return shown && <button type="button">{name}</button>;
}

// The dismiss button of the toast showing `message`.
function dismissButton(message: string): HTMLElement {
  return within(
    screen.getByText(message).closest(".toast") as HTMLElement,
  ).getByRole("button");
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

// Only focus the browser shows, from the keyboard, holds the stack; a click or
// a tap leaves focus on the button it hits, with no ring. The test DOMs show
// every focus, so each side is stubbed where it matters.
function keyboardFocus() {
  vi.spyOn(focusVisible, "isFocusVisible").mockReturnValue(true);
}
function pointerFocus() {
  vi.spyOn(focusVisible, "isFocusVisible").mockReturnValue(false);
}

describe("Toast", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
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

  // The role alone sets the live region: an explicit aria-live on top of it
  // double-announces in VoiceOver.
  it("marks an error toast as an alert", () => {
    renderToasts();
    show("Failed", "error");

    const toast = screen.getByRole("alert");
    expect(toast).toHaveTextContent("Failed");
    expect(toast).not.toHaveAttribute("aria-live");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("describes its dismiss button by the message", () => {
    renderToasts();
    show("Failed", "error");

    expect(dismissButton("Failed")).toHaveAccessibleDescription("Failed");
  });

  it("marks a success toast as a status", () => {
    renderToasts();
    show("Saved", "success");

    const toast = screen.getByRole("status");
    expect(toast).toHaveTextContent("Saved");
    expect(toast).not.toHaveAttribute("aria-live");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("pauses while hovered and resumes with the time left", () => {
    renderToasts();
    show("x".repeat(50), "success"); // 7 s

    advance(3000);
    fireEvent.pointerEnter(screen.getByRole("status"));
    advance(60_000);
    expect(screen.getByRole("status")).toBeInTheDocument();

    fireEvent.pointerLeave(screen.getByRole("status"));
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
    fireEvent.pointerEnter(screen.getByRole("status"));
    act(() => close.focus());
    fireEvent.pointerLeave(screen.getByRole("status"));
    advance(60_000);
    expect(screen.getByRole("status")).toBeInTheDocument();

    act(() => close.blur());
    advance(3999);
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("resumes with the time left after pausing twice", () => {
    renderToasts();
    show("x".repeat(50), "success"); // 7 s

    advance(1000);
    fireEvent.pointerEnter(screen.getByRole("status"));
    advance(10_000);
    fireEvent.pointerLeave(screen.getByRole("status"));
    advance(2000);
    fireEvent.pointerEnter(screen.getByRole("status"));
    advance(10_000);
    fireEvent.pointerLeave(screen.getByRole("status"));

    advance(3999);
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("pauses every toast while the pointer is over any of them", () => {
    renderToasts();
    show("Saved", "success"); // 3.2 s
    show("x".repeat(50), "success"); // 7 s

    advance(1000);
    fireEvent.pointerEnter(screen.getByText("x".repeat(50)));
    advance(60_000);
    expect(screen.getAllByRole("status")).toHaveLength(2);

    fireEvent.pointerLeave(screen.getByText("x".repeat(50)));
    advance(2199);
    expect(screen.getByText("Saved")).toBeInTheDocument();
    advance(1);
    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
    advance(3799);
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("pauses every toast while focus is in any of them", () => {
    renderToasts();
    show("Saved", "success"); // 3.2 s
    show("Failed", "error");
    const close = within(screen.getByRole("alert")).getByRole("button");

    advance(1000);
    act(() => close.focus());
    advance(60_000);
    expect(screen.getByText("Saved")).toBeInTheDocument();

    act(() => close.blur());
    advance(2199);
    expect(screen.getByText("Saved")).toBeInTheDocument();
    advance(1);
    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
  });

  it("stays paused as keyboard focus moves on from a dismissed toast to the next", () => {
    keyboardFocus();
    renderToasts();
    show("Saved", "success"); // 3.2 s
    show("Failed", "error");
    const close = dismissButton("Failed");

    advance(1000);
    act(() => close.focus());
    fireEvent.keyDown(close, { key: "Escape" });

    expect(document.activeElement).toBe(dismissButton("Saved"));
    advance(60_000);
    expect(screen.getByText("Saved")).toBeInTheDocument();
  });

  it("does not pause for focus a click leaves in the stack", () => {
    pointerFocus();
    renderToasts();
    show("Saved", "success"); // 3.2 s
    show("Failed", "error");

    act(() => dismissButton("Failed").focus());

    advance(3200);
    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
  });

  it("does not pause once a click on × hands focus on to the next toast", () => {
    pointerFocus();
    renderToasts();
    show("Saved", "success"); // 3.2 s
    show("Failed", "error");

    act(() => dismissButton("Failed").focus());
    fireEvent.click(dismissButton("Failed"));

    expect(document.activeElement).toBe(dismissButton("Saved"));
    advance(3200);
    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
  });

  it("resumes once focus leaves the stack with the last toast", () => {
    renderToasts(<button type="button">Page</button>);
    show("Failed", "error");
    const close = dismissButton("Failed");

    act(() => screen.getByRole("button", { name: "Page" }).focus());
    act(() => close.focus());
    fireEvent.keyDown(close, { key: "Escape" });

    show("Saved", "success");
    advance(3199);
    expect(screen.getByText("Saved")).toBeInTheDocument();
    advance(1);
    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
  });

  it("resumes once the last toast goes from under the pointer", () => {
    renderToasts();
    show("Failed", "error");
    fireEvent.pointerEnter(screen.getByRole("alert"));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    show("Saved", "success");
    advance(3200);
    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
  });

  it("resumes after a tap dismisses one toast while another shows", () => {
    renderToasts();
    show("Saved", "success"); // 3.2 s
    show("Failed", "error");
    const error = screen.getByRole("alert");

    // A tap: the pointer enters and leaves as the finger lifts, then the
    // compatibility mouse events fire with no mouseout to follow.
    fireEvent.pointerEnter(error);
    fireEvent.pointerLeave(error);
    fireEvent.mouseEnter(error);
    fireEvent.click(within(error).getByRole("button"));

    advance(3199);
    expect(screen.getByText("Saved")).toBeInTheDocument();
    advance(1);
    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
  });

  it("scrolls a raised toast into view", () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    renderToasts();
    show("Failed 1", "error");
    show("Failed 2", "error");

    expect(scroll).toHaveBeenLastCalledWith({ block: "nearest" });
    expect(scroll.mock.contexts.at(-1)).toBe(
      screen.getByText("Failed 2").closest(".toast"),
    );

    show("Failed 1", "error");
    expect(scroll.mock.contexts.at(-1)).toBe(
      screen.getByText("Failed 1").closest(".toast"),
    );
  });

  it("does not scroll a raised toast into view while keyboard focus is in the stack", () => {
    keyboardFocus();
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    renderToasts();
    show("Failed 1", "error");
    act(() => screen.getByRole("button", { name: "Close" }).focus());
    scroll.mockClear();

    show("Failed 2", "error");
    show("Failed 1", "error");

    expect(scroll).not.toHaveBeenCalled();
  });

  it("scrolls a raised toast into view while focus a click left is in the stack", () => {
    pointerFocus();
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    renderToasts();
    show("Failed 1", "error");
    act(() => dismissButton("Failed 1").focus());
    scroll.mockClear();

    show("Failed 2", "error");

    expect(scroll.mock.contexts.at(-1)).toBe(
      screen.getByText("Failed 2").closest(".toast"),
    );
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

  it("leaves the toast up and passes on a key other than Escape", () => {
    const onDocumentKeyDown = vi.fn();
    document.addEventListener("keydown", onDocumentKeyDown);
    try {
      renderToasts();
      show("Failed", "error");
      const close = screen.getByRole("button", { name: "Close" });

      act(() => close.focus());
      fireEvent.keyDown(close, { key: "Tab" });

      expect(screen.getByText("Failed")).toBeInTheDocument();
      expect(onDocumentKeyDown).toHaveBeenCalledTimes(1);
    } finally {
      document.removeEventListener("keydown", onDocumentKeyDown);
    }
  });

  it("shows at most two timed toasts, dropping the oldest", () => {
    renderToasts();
    for (const n of [1, 2, 3]) show(`Saved ${n}`, "success");

    expect(screen.getAllByRole("status")).toHaveLength(2);
    expect(screen.queryByText("Saved 1")).not.toBeInTheDocument();
    expect(screen.getByText("Saved 3")).toBeInTheDocument();
  });

  it("shows at most three errors, dropping the oldest", () => {
    renderToasts();
    for (const n of [1, 2, 3, 4]) show(`Failed ${n}`, "error");

    expect(screen.getAllByRole("alert")).toHaveLength(3);
    expect(screen.queryByText("Failed 1")).not.toBeInTheDocument();
    expect(screen.getByText("Failed 4")).toBeInTheDocument();
  });

  it("drops a toast only for a newer toast of its own kind", () => {
    renderToasts();
    for (const n of [1, 2, 3]) show(`Failed ${n}`, "error");
    for (const n of [1, 2]) show(`Saved ${n}`, "success");

    show("Saved 3", "success");
    expect(screen.getAllByRole("alert")).toHaveLength(3);
    expect(screen.queryByText("Saved 1")).not.toBeInTheDocument();

    show("Failed 4", "error");
    expect(screen.getAllByRole("status")).toHaveLength(2);
    expect(screen.queryByText("Failed 1")).not.toBeInTheDocument();
  });

  it("collapses a repeated error into the one showing, with a count", () => {
    renderToasts();
    show("Failed", "error");
    show("Failed", "error");
    show("Failed", "error");

    const [toast] = screen.getAllByRole("alert");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(toast).toHaveTextContent("Failed");
    expect(
      within(toast).getByText(t("common.toastRepeated", { count: 3 })),
    ).toBeInTheDocument();
    // The visible number is for sighted users; screen readers get the text.
    expect(within(toast).getByText("3")).toHaveAttribute("aria-hidden", "true");
  });

  it("shows no count on an error raised once", () => {
    renderToasts();
    show("Failed", "error");

    expect(
      within(screen.getByRole("alert")).queryByText(
        t("common.toastRepeated", { count: 1 }),
      ),
    ).not.toBeInTheDocument();
  });

  it("counts a repeated error as the newest when dropping", () => {
    renderToasts();
    for (const n of [1, 2, 3]) show(`Failed ${n}`, "error");
    show("Failed 1", "error");

    show("Failed 4", "error");

    expect(screen.getByText("Failed 1")).toBeInTheDocument();
    expect(screen.queryByText("Failed 2")).not.toBeInTheDocument();
  });

  it("keeps a success and an error with the same text apart", () => {
    renderToasts();
    show("Done", "success");
    show("Done", "error");
    show("Done", "success");

    expect(screen.getAllByRole("status")).toHaveLength(2);
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("does not collapse a repeated timed toast", () => {
    renderToasts();
    show("Saved", "success");
    show("Saved", "success");

    expect(screen.getAllByRole("status")).toHaveLength(2);
  });
});

// WEBUI_SPEC.md 7.4: when the toast holding focus goes, focus moves on to the
// next toast, else back where it came from, else to a fallback; never to the
// page body.
describe("Toast focus when the focused toast goes", () => {
  it("moves to the next toast", () => {
    renderToasts();
    for (const n of [1, 2, 3]) show(`Failed ${n}`, "error");

    act(() => dismissButton("Failed 2").focus());
    fireEvent.click(dismissButton("Failed 2"));

    expect(document.activeElement).toBe(dismissButton("Failed 3"));
  });

  it("moves to the toast before when the last one goes", () => {
    renderToasts();
    show("Failed 1", "error");
    show("Failed 2", "error");

    act(() => dismissButton("Failed 2").focus());
    fireEvent.keyDown(dismissButton("Failed 2"), { key: "Escape" });

    expect(document.activeElement).toBe(dismissButton("Failed 1"));
  });

  it("returns to where it came from when no toast is left", () => {
    renderToasts(<button type="button">Page</button>);
    show("Failed 1", "error");
    show("Failed 2", "error");
    const page = screen.getByRole("button", { name: "Page" });

    act(() => page.focus());
    act(() => dismissButton("Failed 1").focus());
    act(() => dismissButton("Failed 2").focus());
    fireEvent.click(dismissButton("Failed 2"));
    fireEvent.click(dismissButton("Failed 1"));

    expect(document.activeElement).toBe(page);
  });

  it("falls back to main when the element it came from is gone", () => {
    renderToasts(
      <main>
        <RemovableButton name="Page" />
      </main>,
    );
    show("Failed", "error");

    act(() => screen.getByRole("button", { name: "Page" }).focus());
    act(() => dismissButton("Failed").focus());
    act(() => removeButton());
    fireEvent.click(dismissButton("Failed"));

    const main = screen.getByRole("main");
    expect(document.activeElement).toBe(main);
    // What lets a browser focus it; happy-dom does not check.
    expect(main).toHaveAttribute("tabindex", "-1");

    // Focusable only to take this focus: a click in the page later must not
    // land on it.
    act(() => main.blur());
    expect(main).not.toHaveAttribute("tabindex");
  });

  it("keeps where it came from across a blur to nothing", () => {
    renderToasts(<button type="button">Page</button>);
    show("Failed", "error");
    const page = screen.getByRole("button", { name: "Page" });
    const close = dismissButton("Failed");

    act(() => page.focus());
    act(() => close.focus());
    // As when the window loses focus and gets it back.
    act(() => close.blur());
    act(() => close.focus());
    fireEvent.click(close);

    expect(document.activeElement).toBe(page);
  });

  it("forgets where it came from once it has left the stack for another element", () => {
    renderToasts(
      <main>
        <button type="button">Page 1</button>
        <button type="button">Page 2</button>
      </main>,
    );
    show("Failed", "error");
    const close = dismissButton("Failed");

    act(() => screen.getByRole("button", { name: "Page 1" }).focus());
    act(() => close.focus());
    act(() => screen.getByRole("button", { name: "Page 2" }).focus());
    // Back from nothing, as after a click on the page.
    act(() => (document.activeElement as HTMLElement).blur());
    act(() => close.focus());
    fireEvent.click(close);

    expect(document.activeElement).toBe(screen.getByRole("main"));
  });

  it("forgets where it came from once it has been handed back there", () => {
    renderToasts(
      <main>
        <button type="button">Page</button>
      </main>,
    );
    const page = screen.getByRole("button", { name: "Page" });
    show("Failed 1", "error");

    act(() => page.focus());
    act(() => dismissButton("Failed 1").focus());
    fireEvent.click(dismissButton("Failed 1"));
    expect(document.activeElement).toBe(page);

    show("Failed 2", "error");
    // Back from nothing, as after a click on the page.
    act(() => page.blur());
    act(() => dismissButton("Failed 2").focus());
    fireEvent.click(dismissButton("Failed 2"));

    expect(document.activeElement).toBe(screen.getByRole("main"));
  });

  it("is left alone when the toast that goes does not hold it", () => {
    renderToasts(<button type="button">Page</button>);
    show("Failed 1", "error");
    show("Failed 2", "error");
    const page = screen.getByRole("button", { name: "Page" });
    const onFocusOut = vi.fn();
    page.addEventListener("focusout", onFocusOut);

    act(() => page.focus());
    fireEvent.click(dismissButton("Failed 1"));

    expect(document.activeElement).toBe(page);
    // Not even moved away and back, which a screen reader would announce.
    expect(onFocusOut).not.toHaveBeenCalled();
  });
});

describe("Toasts over an open modal", () => {
  function renderModal(onClose: () => void, inside: ReactNode) {
    renderToasts(
      <main>
        <Modal open={true} title="Title" onClose={onClose}>
          {inside}
        </Modal>
      </main>,
    );
  }

  // Reaches the toasts the way a keyboard does: Tab from the dialog's last
  // control.
  function tabToToasts() {
    const inside = screen.getByRole("button", { name: "Inside" });
    act(() => inside.focus());
    fireEvent.keyDown(inside, { key: "Tab" });
  }

  it("dismisses only the toast on an Escape and returns focus to the dialog", () => {
    const onClose = vi.fn();
    renderModal(onClose, <button type="button">Inside</button>);
    show("Failed", "error");

    tabToToasts();
    expect(document.activeElement).toBe(dismissButton("Failed"));
    fireEvent.keyDown(dismissButton("Failed"), { key: "Escape" });

    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Inside" }),
    );
  });

  it("closes the modal on an Escape elsewhere in it", () => {
    const onClose = vi.fn();
    renderModal(onClose, <button type="button">Inside</button>);
    show("Failed", "error");
    const inside = screen.getByRole("button", { name: "Inside" });

    act(() => inside.focus());
    fireEvent.keyDown(inside, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("moves focus to the next toast when the cap drops the focused one", () => {
    renderModal(() => {}, <button type="button">Inside</button>);
    for (const n of [1, 2, 3]) show(`Failed ${n}`, "error");

    tabToToasts();
    expect(document.activeElement).toBe(dismissButton("Failed 1"));
    show("Failed 4", "error");

    expect(screen.queryByText("Failed 1")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(dismissButton("Failed 2"));
  });

  it("does not return focus to an element behind the modal", () => {
    renderToasts(
      <main>
        <button type="button">Behind</button>
        <Modal open={true} title="Title" onClose={() => {}}>
          <button type="button">Inside</button>
        </Modal>
      </main>,
    );
    show("Failed", "error");

    // Focus that got behind the modal (#3883), then into the stack.
    act(() => screen.getByRole("button", { name: "Behind" }).focus());
    act(() => dismissButton("Failed").focus());
    fireEvent.click(dismissButton("Failed"));

    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });

  it("falls back to the modal when the element focus came from is gone", () => {
    renderModal(() => {}, <RemovableButton name="Inside" />);
    show("Failed", "error");

    tabToToasts();
    act(() => removeButton());
    fireEvent.click(dismissButton("Failed"));

    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });
});
