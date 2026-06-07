import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionProvider } from "../auth/SessionProvider.tsx";
import { resetForTesting } from "../auth/session.ts";
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
        <SessionProvider>
          <Routes>
            <Route path="/register" element={<RegisterPage />} />
            <Route path="*" element={<PathProbe />} />
          </Routes>
        </SessionProvider>
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

// A successful POST /api/users 201 response body.
function registeredResponse(): Response {
  return new Response(
    JSON.stringify({
      id: "u1",
      username: "alice",
      email: "alice@example.com",
      is_platform_admin: false,
    }),
    { status: 201, headers: { "content-type": "application/json" } },
  );
}

// Route fetches by URL so the SessionProvider's bootstrap probe
// (POST /api/auth/session) does not consume the per-test register/login mocks.
// The bootstrap resolves signed-out; the register and login responses are queued
// per test.
const usersQueue: Response[] = [];
const loginQueue: Response[] = [];

function queueUsers(response: Response) {
  usersQueue.push(response);
}

function queueLogin(response: Response) {
  loginQueue.push(response);
}

const fetchMock = vi.fn((input: RequestInfo | URL) => {
  const url = typeof input === "string" ? input : input.toString();
  if (url.endsWith("/api/auth/session")) {
    // Bootstrap probe: resolve signed-out so it stays out of the way.
    return Promise.resolve(new Response(null, { status: 401 }));
  }
  if (url.endsWith("/api/users")) {
    const next = usersQueue.shift();
    if (next === undefined) throw new Error(`unqueued POST ${url}`);
    return Promise.resolve(next);
  }
  if (url.endsWith("/api/auth/login")) {
    const next = loginQueue.shift();
    if (next === undefined) throw new Error(`unqueued POST ${url}`);
    return Promise.resolve(next);
  }
  throw new Error(`unexpected fetch ${url}`);
});

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockClear();
  usersQueue.length = 0;
  loginQueue.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetForTesting();
});

function submit() {
  fireEvent.click(screen.getByRole("button", { name: t("register.submit") }));
}

describe("RegisterPage", () => {
  it("surfaces a server 422 weak-password reason inline", async () => {
    queueUsers(
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
    queueUsers(
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
    queueUsers(
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
    queueUsers(
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
    // Submission is blocked before any /api/users request goes out.
    expect(fetchMock).not.toHaveBeenCalledWith("/api/users", expect.anything());
  });

  it("auto-logs in and lands on the dashboard after a 201 (#537)", async () => {
    queueUsers(registeredResponse());
    queueLogin(
      new Response(
        JSON.stringify({
          access_token: "tok",
          refresh_token: "ref",
          token_type: "bearer",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    renderRegister();
    fillValid();
    submit();

    // Auto-login navigates to the landing path, not back to /login.
    await waitFor(() =>
      expect(screen.getByTestId("path")).toHaveTextContent("/"),
    );
    expect(screen.queryByTestId("path")).not.toHaveTextContent("/login");
    // The login was performed with the just-entered credentials.
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/login",
      expect.objectContaining({
        body: JSON.stringify({
          username: "alice",
          password: "longenoughpassword",
        }),
      }),
    );
  });

  it("falls back to /login with a notice when the auto-login fails (#537)", async () => {
    queueUsers(registeredResponse());
    // The login step fails (e.g. a registration-policy race); the user is routed
    // to /login with the success notice rather than shown a scary error.
    queueLogin(new Response(null, { status: 401 }));
    renderRegister();
    fillValid();
    submit();

    await waitFor(() =>
      expect(screen.getByTestId("path")).toHaveTextContent("/login"),
    );
    expect(screen.getByText(t("register.success"))).toBeInTheDocument();
  });
});
