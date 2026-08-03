// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Popover } from "./Popover.tsx";

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
