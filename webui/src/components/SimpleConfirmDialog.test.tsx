import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
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

// Goes busy as soon as it is confirmed, as a call site with a mutation does.
function BusyOnConfirm() {
  const [busy, setBusy] = useState(false);
  return (
    <SimpleConfirmDialog
      open={true}
      title="Remove member"
      body="This cannot be undone."
      confirmLabel="Remove"
      busy={busy}
      onConfirm={() => setBusy(true)}
      onClose={vi.fn()}
    />
  );
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

  // The confirm button holds focus when it goes busy; focus stays in the
  // dialog rather than dropping to the page body (WEBUI_SPEC.md 7.4).
  it("keeps focus in the dialog when confirming makes it busy", async () => {
    render(<BusyOnConfirm />);
    const confirmButton = screen.getByRole("button", { name: "Remove" });
    confirmButton.focus();

    fireEvent.click(confirmButton);
    await act(async () => {});

    expect(confirmButton).toBeDisabled();
    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });
});
