import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ConfirmDialog } from "./ConfirmDialog.tsx";

function renderDialog(onConfirm = vi.fn(), onClose = vi.fn()) {
  render(
    <ConfirmDialog
      open={true}
      title="Delete server"
      body="This cannot be undone."
      confirmPhrase="survival"
      confirmLabel="Delete"
      promptLabel="Type the server name"
      onConfirm={onConfirm}
      onClose={onClose}
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

  it("calls onClose when Escape is pressed without tabbing into the dialog", () => {
    const onClose = vi.fn();
    renderDialog(vi.fn(), onClose);

    fireEvent.keyDown(document, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

function renderPasswordDialog({
  username,
  onConfirm = vi.fn(),
  onClose = vi.fn(),
}: {
  username?: string;
  onConfirm?: (password: string) => void;
  onClose?: () => void;
} = {}) {
  render(
    <ConfirmDialog
      open={true}
      title="Delete account"
      body="This cannot be undone."
      confirmPhrase="miura"
      confirmLabel="Delete"
      promptLabel="Type your username"
      passwordLabel="Password"
      username={username}
      onConfirm={onConfirm}
      onClose={onClose}
    />,
  );
  // The label wraps the show/hide toggle too, so pin the query to the input.
  const password = screen.getByLabelText("Password", {
    selector: "input",
  }) as HTMLInputElement;
  return {
    phrase: screen.getByLabelText("Type your username"),
    password,
    confirmButton: screen.getByRole("button", {
      name: "Delete",
    }) as HTMLButtonElement,
  };
}

function fillBoth(phrase: HTMLElement, password: HTMLElement) {
  fireEvent.change(phrase, { target: { value: "miura" } });
  fireEvent.change(password, { target: { value: "MyPass123!" } });
}

// Password managers offer a saved credential to a password field in a form
// that also names the account's username, as the change-password form does
// (#4093, #4121).
describe("ConfirmDialog with a password prompt", () => {
  it("names the password field in a form", () => {
    const { password } = renderPasswordDialog({ username: "miura" });

    expect(password.form).not.toBeNull();
    expect(password).toHaveAttribute("name", "current-password");
    expect(password).toHaveAttribute("autocomplete", "current-password");
  });

  it("carries the username in a hidden read-only field of the password's form", () => {
    const { password } = renderPasswordDialog({ username: "miura" });

    expect(password.form).not.toBeNull();
    const username = password.form?.querySelector(
      'input[autocomplete="username"]',
    );
    expect(username).not.toBeNull();
    expect(username).toHaveAttribute("type", "text");
    expect(username).toHaveAttribute("name", "username");
    expect(username).toHaveValue("miura");
    expect(username).toHaveAttribute("readonly");
    expect(username).toHaveAttribute("tabindex", "-1");
    expect(username).toHaveAttribute("aria-hidden", "true");
    expect(username).toHaveClass("visually-hidden");
  });

  // A script's form.submit() fires no submit event, so a GET form would put
  // the named credentials in the URL (#4180).
  it("posts the password's form", () => {
    const { password } = renderPasswordDialog({ username: "miura" });

    expect(password.form).toHaveAttribute("method", "post");
  });

  // On account deletion the phrase is the username, so a filled phrase field
  // would bypass the typed confirm (#4180).
  it("keeps the typed phrase out of autofill and form history", () => {
    const { phrase } = renderPasswordDialog({ username: "miura" });

    expect(phrase).toHaveAttribute("autocomplete", "off");
    expect(phrase).not.toHaveAttribute("name");
  });

  // Out of the sign-in form, and before it, so the hidden username field is
  // never the dialog's first control (#4121, #4180).
  it("keeps the typed phrase outside the password's form, before it", () => {
    const { phrase, password } = renderPasswordDialog({ username: "miura" });

    const form = password.form as HTMLFormElement;
    expect((phrase as HTMLInputElement).form).toBeNull();
    expect(
      phrase.compareDocumentPosition(form) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
  });

  it("renders no username field when the caller passes no username", () => {
    const { password } = renderPasswordDialog();

    expect(password.form).not.toBeNull();
    expect(
      screen.getByRole("dialog").querySelector('input[name="username"]'),
    ).toBeNull();
  });

  // Enter in a field submits its form through the form's submit button, which
  // happy-dom and jsdom do not implement; so this pins that the confirm button
  // is that submit button, and that a submit confirms.
  it("confirms with the password on a submit of its form, as Enter does", () => {
    const onConfirm = vi.fn();
    const { phrase, password, confirmButton } = renderPasswordDialog({
      username: "miura",
      onConfirm,
    });
    fillBoth(phrase, password);

    expect(password.form).not.toBeNull();
    expect(confirmButton).toHaveAttribute("type", "submit");
    expect(confirmButton.form).toBe(password.form);
    fireEvent.submit(password.form as HTMLFormElement);

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith("MyPass123!");
  });

  // A password manager may fill the password and submit the form itself, with
  // the phrase still untyped. The submit never leaves the page either way.
  it("ignores a submit while the phrase does not match", () => {
    const onConfirm = vi.fn();
    const { password } = renderPasswordDialog({
      username: "miura",
      onConfirm,
    });
    fireEvent.change(password, { target: { value: "MyPass123!" } });

    expect(password.form).not.toBeNull();
    const notPrevented = fireEvent.submit(password.form as HTMLFormElement);

    expect(onConfirm).not.toHaveBeenCalled();
    expect(notPrevented).toBe(false);
  });

  it("ignores a submit while the password is empty", () => {
    const onConfirm = vi.fn();
    const { phrase, password } = renderPasswordDialog({
      username: "miura",
      onConfirm,
    });
    fireEvent.change(phrase, { target: { value: "miura" } });

    expect(password.form).not.toBeNull();
    const notPrevented = fireEvent.submit(password.form as HTMLFormElement);

    expect(onConfirm).not.toHaveBeenCalled();
    expect(notPrevented).toBe(false);
  });

  it("confirms once on a click of the confirm button", () => {
    const onConfirm = vi.fn();
    const { phrase, password, confirmButton } = renderPasswordDialog({
      username: "miura",
      onConfirm,
    });
    fillBoth(phrase, password);

    fireEvent.click(confirmButton);

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith("MyPass123!");
  });

  it("closes on cancel without confirming", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    const { phrase, password } = renderPasswordDialog({
      username: "miura",
      onConfirm,
      onClose,
    });
    fillBoth(phrase, password);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("closes on Escape without confirming", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    const { phrase, password } = renderPasswordDialog({
      username: "miura",
      onConfirm,
      onClose,
    });
    fillBoth(phrase, password);

    fireEvent.keyDown(password, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
