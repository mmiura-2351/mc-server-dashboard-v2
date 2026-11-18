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
});
