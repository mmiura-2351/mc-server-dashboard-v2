import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SimpleConfirmDialog } from "./SimpleConfirmDialog.tsx";

function renderDialog(props: { busy?: boolean; onConfirm?: () => void } = {}) {
  render(
    <SimpleConfirmDialog
      open={true}
      title="Remove member"
      body="This cannot be undone."
      confirmLabel="Remove"
      busy={props.busy}
      onConfirm={props.onConfirm ?? vi.fn()}
      onClose={vi.fn()}
    />,
  );
  return { confirmButton: screen.getByRole("button", { name: "Remove" }) };
}

describe("SimpleConfirmDialog", () => {
  it("enables the confirm button when not busy", () => {
    const { confirmButton } = renderDialog();
    expect(confirmButton).toBeEnabled();
  });

  it("disables the confirm button while busy", () => {
    const onConfirm = vi.fn();
    const { confirmButton } = renderDialog({ busy: true, onConfirm });

    expect(confirmButton).toBeDisabled();

    fireEvent.click(confirmButton);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
