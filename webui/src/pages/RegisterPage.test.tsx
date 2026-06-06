import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import { RegisterPage } from "./RegisterPage.tsx";

function PathProbe() {
  return <span data-testid="path">{useLocation().pathname}</span>;
}

function renderRegister() {
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={["/register"]}>
        <Routes>
          <Route path="/register" element={<RegisterPage />} />
          <Route path="*" element={<PathProbe />} />
        </Routes>
      </MemoryRouter>
    </ToastProvider>,
  );
}

// Fill the form with values that pass the client-side checks so submission
// reaches the network, where the test controls the response.
function fillValid() {
  fireEvent.change(screen.getByLabelText(t("auth.fieldUsername")), {
    target: { value: "alice" },
  });
  fireEvent.change(screen.getByLabelText(t("auth.fieldEmail")), {
    target: { value: "alice@example.com" },
  });
  fireEvent.change(screen.getByLabelText(t("auth.fieldPassword")), {
    target: { value: "longenoughpassword" },
  });
  fireEvent.change(screen.getByLabelText(t("register.confirmPassword")), {
    target: { value: "longenoughpassword" },
  });
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function submit() {
  fireEvent.click(screen.getByRole("button", { name: t("register.submit") }));
}

describe("RegisterPage", () => {
  it("surfaces a server 422 weak-password reason inline", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ reason: "simple_pattern", status: 422 }), {
        status: 422,
        headers: { "content-type": "application/problem+json" },
      }),
    );
    renderRegister();
    fillValid();
    submit();

    expect(
      await screen.findByText(t("register.reason.simple_pattern")),
    ).toBeInTheDocument();
  });

  it("maps a structural 422 validation_error to inline field errors", async () => {
    // Empty username/email with a long-enough password passes localValidate and
    // submits, where Pydantic's min_length=1 rejects it as a structural
    // validation_error carrying the per-field errors list (#410, #395 shape:
    // loc/msg/type, input/ctx scrubbed).
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          reason: "validation_error",
          status: 422,
          errors: [
            {
              loc: ["body", "username"],
              msg: "String should have at least 1 character",
              type: "string_too_short",
            },
            {
              loc: ["body", "email"],
              msg: "value is not a valid email address",
              type: "value_error",
            },
          ],
        }),
        {
          status: 422,
          headers: { "content-type": "application/problem+json" },
        },
      ),
    );
    renderRegister();
    fillValid();
    submit();

    expect(
      await screen.findByText("String should have at least 1 character"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("value is not a valid email address"),
    ).toBeInTheDocument();
  });

  it("falls back to the generic toast for an unmappable structural 422", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          reason: "validation_error",
          status: 422,
          errors: [
            {
              loc: ["body"],
              msg: "Input should be a valid dictionary",
              type: "model_type",
            },
          ],
        }),
        {
          status: 422,
          headers: { "content-type": "application/problem+json" },
        },
      ),
    );
    renderRegister();
    fillValid();
    submit();

    expect(
      await screen.findByText(t("register.genericError")),
    ).toBeInTheDocument();
  });

  it("surfaces a username-taken conflict against the username field", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ reason: "username_taken", status: 409 }), {
        status: 409,
        headers: { "content-type": "application/problem+json" },
      }),
    );
    renderRegister();
    fillValid();
    submit();

    expect(
      await screen.findByText(t("register.reason.username_taken")),
    ).toBeInTheDocument();
  });

  it("blocks submission with a client-side too-short password", () => {
    renderRegister();
    fireEvent.change(screen.getByLabelText(t("auth.fieldUsername")), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText(t("auth.fieldEmail")), {
      target: { value: "alice@example.com" },
    });
    fireEvent.change(screen.getByLabelText(t("auth.fieldPassword")), {
      target: { value: "short" },
    });
    submit();

    expect(
      screen.getByText(t("register.reason.too_short")),
    ).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("routes to /login with a success notice on a 201", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          id: "u1",
          username: "alice",
          email: "alice@example.com",
          is_platform_admin: false,
        }),
        { status: 201, headers: { "content-type": "application/json" } },
      ),
    );
    renderRegister();
    fillValid();
    submit();

    await waitFor(() =>
      expect(screen.getByTestId("path")).toHaveTextContent("/login"),
    );
    expect(screen.getByText(t("register.success"))).toBeInTheDocument();
  });
});
