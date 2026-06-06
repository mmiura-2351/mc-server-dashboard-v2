import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ConfirmDialog } from "./ConfirmDialog.tsx";

function renderDialog(onConfirm = vi.fn()) {
  render(
    <ConfirmDialog
      open={true}
      title="Delete server"
      body="This cannot be undone."
      confirmPhrase="survival"
      confirmLabel="Delete"
      promptLabel="Type the server name"
      onConfirm={onConfirm}
      onClose={() => {}}
    />,
  );
  return {
    input: screen.getByRole("textbox"),
    confirmButton: screen.getByRole("button", { name: "Delete" }),
  };
}

describe("ConfirmDialog", () => {
  it("disables the confirm button until the phrase matches exactly", () => {
    const { input, confirmButton } = renderDialog();

    expect(confirmButton).toBeDisabled();

    fireEvent.change(input, { target: { value: "surviva" } });
    expect(confirmButton).toBeDisabled();

    fireEvent.change(input, { target: { value: "survival" } });
    expect(confirmButton).toBeEnabled();
  });

  it("treats the phrase as case-sensitive", () => {
    const { input, confirmButton } = renderDialog();

    fireEvent.change(input, { target: { value: "Survival" } });
    expect(confirmButton).toBeDisabled();
  });

  it("calls onConfirm only once the phrase matches", () => {
    const onConfirm = vi.fn();
    const { input, confirmButton } = renderDialog(onConfirm);

    fireEvent.change(input, { target: { value: "survival" } });
    fireEvent.click(confirmButton);

    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
