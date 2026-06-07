import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PasswordInput } from "./PasswordInput.tsx";

describe("PasswordInput", () => {
  it("renders a masked password input by default", () => {
    render(<PasswordInput id="pw" value="" onChange={() => {}} />);

    const input = document.getElementById("pw") as HTMLInputElement;
    expect(input.type).toBe("password");
  });

  it("reveals the value when the toggle is pressed and re-masks on a second press", () => {
    render(<PasswordInput id="pw" value="secret" onChange={() => {}} />);

    const input = document.getElementById("pw") as HTMLInputElement;
    const toggle = screen.getByRole("button");

    expect(toggle).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(toggle);
    expect(input.type).toBe("text");
    expect(toggle).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(toggle);
    expect(input.type).toBe("password");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });

  it("labels the toggle for assistive tech and updates the label with state", () => {
    render(<PasswordInput id="pw" value="" onChange={() => {}} />);

    const toggle = screen.getByRole("button");
    const shown = toggle.getAttribute("aria-label");

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-label")).not.toBe(shown);
  });

  it("forwards input props to the underlying field", () => {
    render(
      <PasswordInput
        id="pw"
        value="hi"
        onChange={() => {}}
        autoComplete="new-password"
        placeholder="enter"
        required
      />,
    );

    const input = document.getElementById("pw") as HTMLInputElement;
    expect(input.value).toBe("hi");
    expect(input.autocomplete).toBe("new-password");
    expect(input.placeholder).toBe("enter");
    expect(input.required).toBe(true);
  });
});
