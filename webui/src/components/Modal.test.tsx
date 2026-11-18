import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { type ReactNode, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Modal } from "./Modal.tsx";
import { ToastProvider, useToast } from "./Toast.tsx";

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      <Modal open={open} title="Title" onClose={() => setOpen(false)}>
        <button type="button">Inside</button>
      </Modal>
    </>
  );
}

describe("Modal", () => {
  it("renders nothing when closed", () => {
    render(
      <Modal open={false} title="Title" onClose={() => {}}>
        <p>Body</p>
      </Modal>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders the title and body when open", () => {
    render(
      <Modal open={true} title="Settings" onClose={() => {}}>
        <p>Body content</p>
      </Modal>,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Settings");
    expect(dialog).toHaveTextContent("Body content");
  });

  it("calls onClose when the backdrop is clicked", () => {
    const onClose = vi.fn();
    render(
      <Modal open={true} title="Title" onClose={onClose}>
        <p>Body</p>
      </Modal>,
    );
    fireEvent.click(screen.getByTestId("modal-backdrop"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when Escape is pressed without tabbing into the dialog", () => {
    const onClose = vi.fn();
    render(
      <Modal open={true} title="Title" onClose={onClose}>
        <p>Body</p>
      </Modal>,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not call onClose when the dialog body is clicked", () => {
    const onClose = vi.fn();
    render(
      <Modal open={true} title="Title" onClose={onClose}>
        <p>Body</p>
      </Modal>,
    );
    fireEvent.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("restores focus to the triggering element when closed", () => {
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Open" });
    trigger.focus();
    fireEvent.click(trigger);

    // Focus has moved into the dialog while open.
    expect(screen.getByRole("dialog")).toContainElement(
      document.activeElement as HTMLElement,
    );

    fireEvent.keyDown(document, { key: "Escape" });

    // On close, focus returns to the element that opened the dialog.
    expect(document.activeElement).toBe(trigger);
  });

  it("traps Tab focus within the open dialog (wraps last → first)", () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        <button type="button">First</button>
        <button type="button">Last</button>
      </Modal>,
    );
    const first = screen.getByRole("button", { name: "First" });
    const last = screen.getByRole("button", { name: "Last" });

    last.focus();
    // Cancelled, or the browser would move focus one step further.
    expect(fireEvent.keyDown(document, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(first);
  });

  it("traps Shift+Tab focus within the open dialog (wraps first → last)", () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        <button type="button">First</button>
        <button type="button">Last</button>
      </Modal>,
    );
    const first = screen.getByRole("button", { name: "First" });
    const last = screen.getByRole("button", { name: "Last" });

    first.focus();
    expect(fireEvent.keyDown(document, { key: "Tab", shiftKey: true })).toBe(
      false,
    );
    expect(document.activeElement).toBe(last);
  });

  it("Escape closes only the topmost of stacked dialogs (Files-drawer shape)", () => {
    const onCloseOuter = vi.fn();
    const onCloseInner = vi.fn();
    render(
      <>
        <Modal open={true} title="History" onClose={onCloseOuter}>
          <button type="button">Outer body</button>
        </Modal>
        <Modal open={true} title="Confirm rollback" onClose={onCloseInner}>
          <button type="button">Inner body</button>
        </Modal>
      </>,
    );

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCloseInner).toHaveBeenCalledTimes(1);
    expect(onCloseOuter).not.toHaveBeenCalled();
  });
});

// Focus outside the topmost dialog, on the page body or the page behind,
// re-enters it on the next Tab rather than walking the page (WEBUI_SPEC.md 7.4).
describe("Modal focus from outside the dialog", () => {
  const controls = (
    <>
      <button type="button">First</button>
      <button type="button">Last</button>
    </>
  );

  // Puts focus on the page body, where a browser leaves it after a click on
  // text that takes no focus.
  function focusBody() {
    (document.activeElement as HTMLElement).blur();
    expect(document.activeElement).toBe(document.body);
  }

  it("moves Tab from the page body to the dialog's first control", () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        {controls}
      </Modal>,
    );
    focusBody();

    expect(fireEvent.keyDown(document.body, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "First" }),
    );
  });

  it("moves Shift+Tab from the page body to the dialog's last control", () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        {controls}
      </Modal>,
    );
    focusBody();

    expect(
      fireEvent.keyDown(document.body, { key: "Tab", shiftKey: true }),
    ).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Last" }),
    );
  });

  it("moves Tab from the page behind the dialog into it", () => {
    render(
      <>
        <button type="button">Behind</button>
        <Modal open={true} title="Title" onClose={() => {}}>
          {controls}
        </Modal>
      </>,
    );
    const behind = screen.getByRole("button", { name: "Behind" });
    behind.focus();

    expect(fireEvent.keyDown(behind, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "First" }),
    );
  });

  it("leaves Tab between the dialog's own controls to the browser", () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        {controls}
      </Modal>,
    );
    const first = screen.getByRole("button", { name: "First" });
    first.focus();

    expect(fireEvent.keyDown(first, { key: "Tab" })).toBe(true);
    expect(document.activeElement).toBe(first);
  });

  it("moves Tab from the page body into the topmost of stacked dialogs", () => {
    render(
      <>
        <Modal open={true} title="History" onClose={() => {}}>
          <button type="button">Outer body</button>
        </Modal>
        <Modal open={true} title="Confirm rollback" onClose={() => {}}>
          <button type="button">Inner body</button>
        </Modal>
      </>,
    );
    focusBody();

    expect(fireEvent.keyDown(document.body, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Inner body" }),
    );
  });

  it("moves Tab from a dialog below the topmost one into the topmost", () => {
    render(
      <>
        <Modal open={true} title="History" onClose={() => {}}>
          <button type="button">Outer body</button>
        </Modal>
        <Modal open={true} title="Confirm rollback" onClose={() => {}}>
          <button type="button">Inner body</button>
        </Modal>
      </>,
    );
    const outer = screen.getByRole("button", { name: "Outer body" });
    outer.focus();

    expect(fireEvent.keyDown(outer, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Inner body" }),
    );
  });
});

// A dialog that disables the control holding focus, as a confirm does while
// its action runs, keeps focus: it goes to the dialog, never to the page body
// (WEBUI_SPEC.md 7.4). The test DOM, like Firefox, leaves focus on the disabled
// control; Chromium drops it to the body a frame later.
function BusyFooter() {
  const [busy, setBusy] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setBusy(true)}>
        Start elsewhere
      </button>
      <button type="button" disabled={busy} onClick={() => setBusy(true)}>
        Save
      </button>
    </>
  );
}

function HistoryWithConfirm() {
  const [confirming, setConfirming] = useState(false);
  return (
    <Modal open={true} title="History" onClose={() => {}}>
      <button type="button" onClick={() => setConfirming(true)}>
        Rollback
      </button>
      {confirming && (
        <Modal open={true} title="Confirm rollback" onClose={() => {}}>
          <BusyFooter />
        </Modal>
      )}
    </Modal>
  );
}

describe("Modal focus when its focused control is disabled", () => {
  // The dialog hears about the change on a microtask.
  async function settle() {
    await act(async () => {});
  }

  it("moves focus to the dialog, unscrolled, when the focused control is disabled", async () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        <BusyFooter />
      </Modal>,
    );
    const dialog = screen.getByRole("dialog");
    const focus = vi.spyOn(dialog, "focus");
    const save = screen.getByRole("button", { name: "Save" });
    save.focus();

    fireEvent.click(save);
    await settle();

    expect(save).toBeDisabled();
    expect(document.activeElement).toBe(dialog);
    expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
  });

  it("leaves focus where it is when a control without it is disabled", async () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        <BusyFooter />
      </Modal>,
    );
    const elsewhere = screen.getByRole("button", { name: "Start elsewhere" });
    elsewhere.focus();

    fireEvent.click(elsewhere);
    await settle();

    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(document.activeElement).toBe(elsewhere);
  });

  // The Files history drawer opens its rollback confirm inside its own body,
  // so the outer dialog sees the inner one's changes too.
  it("moves focus to the topmost dialog when a dialog nested inside another disables it", async () => {
    render(<HistoryWithConfirm />);
    fireEvent.click(screen.getByRole("button", { name: "Rollback" }));
    const save = screen.getByRole("button", { name: "Save" });
    save.focus();

    fireEvent.click(save);
    await settle();

    expect(document.activeElement).toBe(
      screen.getByRole("dialog", { name: "Confirm rollback" }),
    );
  });
});

// The toast stack stays reachable while a dialog is open (WEBUI_SPEC.md 7.4):
// Tab runs through the dialog, then the toasts, then wraps.
let raise: (message: string) => void;
function RaiseErrors() {
  const { showToast } = useToast();
  raise = (message) => showToast(message, "error");
  return null;
}

describe("Modal focus trap with toasts showing", () => {
  function renderWithToasts(body: ReactNode) {
    render(
      <ToastProvider>
        <RaiseErrors />
        <Modal open={true} title="Title" onClose={() => {}}>
          {body}
        </Modal>
      </ToastProvider>,
    );
    act(() => {
      raise("Failed 1");
      raise("Failed 2");
    });
  }

  // The modal's backdrop is a "Close" button too, so pick the toasts' own.
  function toastButtons(): HTMLElement[] {
    return screen
      .getAllByRole("alert")
      .map((toast) => within(toast).getByRole("button"));
  }

  // Presses Tab where focus is, and expects the trap to cancel it: at an
  // edge the browser would otherwise move focus one step further.
  function pressTab(shiftKey = false) {
    expect(
      fireEvent.keyDown(document.activeElement ?? document, {
        key: "Tab",
        shiftKey,
      }),
    ).toBe(false);
  }

  const controls = (
    <>
      <button type="button">First</button>
      <button type="button">Last</button>
    </>
  );

  it("moves Tab from the dialog's last control to the first toast", () => {
    renderWithToasts(controls);

    screen.getByRole("button", { name: "Last" }).focus();
    pressTab();

    expect(document.activeElement).toBe(toastButtons()[0]);
  });

  it("wraps Tab from the last toast to the dialog's first control", () => {
    renderWithToasts(controls);

    toastButtons()[1].focus();
    pressTab();

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "First" }),
    );
  });

  it("moves Shift+Tab from the dialog's first control to the last toast", () => {
    renderWithToasts(controls);

    screen.getByRole("button", { name: "First" }).focus();
    pressTab(true);

    expect(document.activeElement).toBe(toastButtons()[1]);
  });

  it("moves Shift+Tab from the dialog itself to the last toast", () => {
    renderWithToasts(controls);

    expect(document.activeElement).toBe(screen.getByRole("dialog"));
    pressTab(true);

    expect(document.activeElement).toBe(toastButtons()[1]);
  });

  it("moves Shift+Tab from the first toast to the dialog's last control", () => {
    renderWithToasts(controls);

    toastButtons()[0].focus();
    pressTab(true);

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Last" }),
    );
  });

  it("moves Tab from a dialog with no controls to the first toast", () => {
    renderWithToasts(<p>Body</p>);

    expect(document.activeElement).toBe(screen.getByRole("dialog"));
    pressTab();

    expect(document.activeElement).toBe(toastButtons()[0]);
  });

  it("wraps Tab from the last toast to the first when the dialog has no controls", () => {
    renderWithToasts(<p>Body</p>);

    toastButtons()[1].focus();
    pressTab();

    expect(document.activeElement).toBe(toastButtons()[0]);
  });

  it("leaves Tab between toasts to the browser", () => {
    renderWithToasts(controls);

    toastButtons()[0].focus();

    expect(fireEvent.keyDown(toastButtons()[0], { key: "Tab" })).toBe(true);
    expect(document.activeElement).toBe(toastButtons()[0]);
  });

  // In a browser a click on a toast's text leaves focus on the page body;
  // the test DOM does not move it, so it is put there by hand.
  it("keeps Shift+Tab within the dialog and the toasts after a click on a toast's text", () => {
    renderWithToasts(controls);
    toastButtons()[0].focus();

    fireEvent.click(screen.getByText("Failed 1"));
    (document.activeElement as HTMLElement).blur();
    expect(document.activeElement).toBe(document.body);
    pressTab(true);

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Last" }),
    );
  });

  it("moves Tab from the page body to a dialog with no controls, not to a toast", () => {
    renderWithToasts(<p>Body</p>);
    (document.activeElement as HTMLElement).blur();
    expect(document.activeElement).toBe(document.body);

    pressTab();

    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });
});
