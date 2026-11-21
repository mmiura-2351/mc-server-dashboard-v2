// @vitest-environment jsdom
// Pinned to jsdom: happy-dom's `:disabled` does not match a control inside a
// disabled fieldset, which is the case pinned here.
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { Modal } from "./Modal.tsx";

it("skips a control disabled by its fieldset at the end of the dialog", () => {
  render(
    <Modal open={true} title="Title" onClose={() => {}}>
      <button type="button">First</button>
      <button type="button">Last</button>
      <fieldset disabled={true}>
        <button type="button">Busy</button>
      </fieldset>
    </Modal>,
  );
  const last = screen.getByRole("button", { name: "Last" });
  last.focus();

  expect(fireEvent.keyDown(last, { key: "Tab" })).toBe(false);
  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "First" }),
  );
});
