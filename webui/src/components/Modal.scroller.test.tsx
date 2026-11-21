// @vitest-environment jsdom
// Pinned to jsdom: its focus() leaves a scroller with no tabindex unfocused,
// as a browser that does not stop Tab on scrollers does, and as Chromium does
// with a disabled textarea. happy-dom focuses any element.
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { expect, it } from "vitest";
import { Modal } from "./Modal.tsx";
import { ToastProvider, useToast } from "./Toast.tsx";

let raise: (message: string) => void;
function RaiseErrors() {
  const { showToast } = useToast();
  raise = (message) => showToast(message, "error");
  return null;
}

// A dialog whose body overflows and holds no enabled control, so the trap
// lists the body as a scroller, over the page, with one error toast up.
function renderOverflowingDialog(footer?: ReactNode) {
  render(
    <ToastProvider>
      <RaiseErrors />
      <Modal open={true} title="Title" onClose={() => {}} footer={footer}>
        <button type="button" disabled>
          Row
        </button>
      </Modal>
      <button type="button">Behind</button>
    </ToastProvider>,
  );
  act(() => raise("Failed"));
  const body = screen
    .getByRole("dialog")
    .querySelector(".modal-body") as HTMLElement;
  body.style.overflowY = "auto";
  Object.defineProperty(body, "scrollHeight", {
    configurable: true,
    value: 600,
  });
  Object.defineProperty(body, "clientHeight", {
    configurable: true,
    value: 200,
  });
}

const toast = () => within(screen.getByRole("alert")).getByRole("button");

// Presses Tab where focus is. A Tab the trap leaves to the browser goes past
// the body, which is no stop here, to `landsOn`. Returns whether the trap
// left it to the browser.
async function tab(landsOn: HTMLElement, shiftKey = false) {
  const from = document.activeElement ?? document.body;
  const toBrowser = fireEvent.keyDown(from, { key: "Tab", shiftKey });
  if (toBrowser) {
    landsOn.focus();
    await act(async () => {});
  }
  fireEvent.keyUp(document.activeElement ?? document.body, {
    key: "Tab",
    shiftKey,
  });
  return toBrowser;
}

it("takes a Tab from a dialog with no enabled control past a scroller that takes no focus, to the toast", async () => {
  renderOverflowingDialog();
  expect(document.activeElement).toBe(screen.getByRole("dialog"));

  // Left to the browser, as the body is listed: past the body it lands on
  // the page behind.
  expect(await tab(screen.getByRole("button", { name: "Behind" }))).toBe(true);

  expect(document.activeElement).toBe(toast());
});

it("takes a Shift+Tab from the footer past a scroller that takes no focus, to the toast", async () => {
  renderOverflowingDialog(<button type="button">Done</button>);
  const done = screen.getByRole("button", { name: "Done" });
  done.focus();

  // Left to the browser, as the body is listed: past the body it lands on
  // the backdrop before the dialog.
  expect(await tab(screen.getByTestId("modal-backdrop"), true)).toBe(true);

  expect(document.activeElement).toBe(toast());
});
