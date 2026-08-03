// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { clampPopoverOffset, Popover } from "./Popover.tsx";

function renderPopover() {
  return render(
    <div>
      <Popover label="Menu">
        <p>panel content</p>
        <button type="button">inside</button>
      </Popover>
      <button type="button">outside</button>
    </div>,
  );
}

describe("Popover", () => {
  it("is closed initially and opens on trigger click", () => {
    renderPopover();
    expect(screen.queryByText("panel content")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    expect(screen.getByText("panel content")).toBeInTheDocument();
  });

  it("reflects open state through aria-expanded on the trigger", () => {
    renderPopover();
    const trigger = screen.getByRole("button", { name: "Menu" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });

  it("toggles closed on a second trigger click", () => {
    renderPopover();
    const trigger = screen.getByRole("button", { name: "Menu" });

    fireEvent.click(trigger);
    expect(screen.getByText("panel content")).toBeInTheDocument();
    fireEvent.mouseDown(trigger);
    fireEvent.click(trigger);
    expect(screen.queryByText("panel content")).not.toBeInTheDocument();
  });

  it("closes on an outside click", () => {
    renderPopover();
    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    expect(screen.getByText("panel content")).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByRole("button", { name: "outside" }));
    expect(screen.queryByText("panel content")).not.toBeInTheDocument();
  });

  it("stays open on a click inside the panel", () => {
    renderPopover();
    fireEvent.click(screen.getByRole("button", { name: "Menu" }));

    fireEvent.mouseDown(screen.getByRole("button", { name: "inside" }));
    expect(screen.getByText("panel content")).toBeInTheDocument();
  });

  it("closes on Escape and returns focus to the trigger", () => {
    renderPopover();
    const trigger = screen.getByRole("button", { name: "Menu" });
    fireEvent.click(trigger);
    expect(screen.getByText("panel content")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText("panel content")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("keeps the panel within the viewport when the trigger sits near the right edge", () => {
    // Regression (#2239 review): at ~375px the trigger sits at x≈289 and a
    // 160px left-aligned panel overflowed the right edge by 74px. The offset
    // shifts it left so its right edge lands within the 8px margin.
    const offset = clampPopoverOffset(289, 160, 375, 8);
    const panelLeft = 289 + offset;
    expect(panelLeft).toBeGreaterThanOrEqual(8);
    expect(panelLeft + 160).toBeLessThanOrEqual(375 - 8);
  });

  it("leaves a left-wrapped trigger's panel un-shifted", () => {
    // At ~360px the bar wraps and the trigger moves to x≈79, where the panel
    // already fits; a naive right-alignment would push it off the LEFT edge.
    expect(clampPopoverOffset(79, 160, 360, 8)).toBe(0);
  });

  it("does not shift a panel that already fits with room to spare", () => {
    expect(clampPopoverOffset(20, 160, 1200, 8)).toBe(0);
  });

  it("degrades to the left margin when the panel is wider than the viewport", () => {
    const offset = clampPopoverOffset(100, 160, 170, 8);
    expect(100 + offset).toBe(8);
  });

  it("passes a close callback to a render-prop child", () => {
    render(
      <Popover label="Menu">
        {(close) => (
          <button type="button" onClick={close}>
            done
          </button>
        )}
      </Popover>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    fireEvent.click(screen.getByRole("button", { name: "done" }));
    expect(
      screen.queryByRole("button", { name: "done" }),
    ).not.toBeInTheDocument();
  });
});
