// @vitest-environment jsdom
// Pinned to jsdom: getByLabelText("Current password") is unambiguous under
// jsdom but matches both the input and the show/hide toggle under happy-dom
// (issue #1751).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import { AccountPage } from "./AccountPage.tsx";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
const mockLogout = vi.hoisted(() => vi.fn());

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return { ...actual, api: mockApi };
});

vi.mock("../auth/SessionProvider.tsx", () => ({
  useSession: () => ({ status: "signed-in", logout: mockLogout }),
}));

const me = {
  id: "u1",
  username: "miura",
  email: "miura@example.com",
  is_platform_admin: false,
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <AccountPage />
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...result, queryClient };
}

beforeEach(() => {
  mockApi.get.mockReset();
  mockApi.patch.mockReset();
  mockApi.put.mockReset();
  mockApi.delete.mockReset();
  mockLogout.mockReset();
  // Default: /users/me then /communities.
  mockApi.get.mockImplementation((path: string) => {
    if (path === "/api/users/me") return Promise.resolve(me);
    if (path === "/api/communities")
      return Promise.resolve([
        { id: "c1", name: "Sakura SMP" },
        { id: "c2", name: "Dev Playground" },
      ]);
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

// Scoped to the profile field: the password form's hidden username field
// carries the same value.
async function waitForLoaded() {
  await waitFor(() =>
    expect(screen.getByLabelText(t("account.profile.username"))).toHaveValue(
      "miura",
    ),
  );
}

describe("AccountPage profile", () => {
  it("renders the current profile and memberships", async () => {
    renderPage();
    await waitForLoaded();

    expect(screen.getByDisplayValue("miura@example.com")).toBeInTheDocument();
    expect(screen.getByText("Sakura SMP")).toBeInTheDocument();
    expect(screen.getByText("Dev Playground")).toBeInTheDocument();
  });

  it("surfaces a memberships load failure instead of the empty state", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/api/users/me") return Promise.resolve(me);
      if (path === "/api/communities")
        return Promise.reject(new ApiError(500, { reason: "internal_error" }));
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    renderPage();
    await waitForLoaded();

    expect(
      await screen.findByText(t("account.memberships.loadError")),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(t("account.memberships.none")),
    ).not.toBeInTheDocument();
  });

  it("saves profile edits and shows a success toast", async () => {
    mockApi.patch.mockResolvedValue({ ...me, username: "miura2" });
    renderPage();
    await waitForLoaded();

    fireEvent.change(screen.getByLabelText(t("account.profile.username")), {
      target: { value: "miura2" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("account.profile.save") }),
    );

    await waitFor(() =>
      expect(mockApi.patch).toHaveBeenCalledWith("/api/users/me", {
        body: JSON.stringify({
          username: "miura2",
          email: "miura@example.com",
        }),
      }),
    );
    expect(
      await screen.findByText(t("account.profile.saved")),
    ).toBeInTheDocument();
  });

  it("surfaces a 409 username conflict inline", async () => {
    mockApi.patch.mockRejectedValue(
      new ApiError(409, { reason: "username_taken" }),
    );
    renderPage();
    await waitForLoaded();

    fireEvent.click(
      screen.getByRole("button", { name: t("account.profile.save") }),
    );

    expect(
      await screen.findByText(t("account.error.username_taken")),
    ).toBeInTheDocument();
  });
});

describe("AccountPage password", () => {
  it("blocks submission when the new passwords do not match", async () => {
    renderPage();
    await waitForLoaded();

    fireEvent.change(screen.getByLabelText(t("account.password.current")), {
      target: { value: "OldPass123!" },
    });
    fireEvent.change(screen.getByLabelText(t("account.password.new")), {
      target: { value: "NewPass123!" },
    });
    fireEvent.change(screen.getByLabelText(t("account.password.confirm")), {
      target: { value: "Different123!" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("account.password.change") }),
    );

    expect(
      await screen.findByText(t("account.password.mismatch")),
    ).toBeInTheDocument();
    expect(mockApi.put).not.toHaveBeenCalled();
  });

  it("changes the password and shows a success toast", async () => {
    mockApi.put.mockResolvedValue(undefined);
    renderPage();
    await waitForLoaded();

    fireEvent.change(screen.getByLabelText(t("account.password.current")), {
      target: { value: "OldPass123!" },
    });
    fireEvent.change(screen.getByLabelText(t("account.password.new")), {
      target: { value: "NewPass123!" },
    });
    fireEvent.change(screen.getByLabelText(t("account.password.confirm")), {
      target: { value: "NewPass123!" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("account.password.change") }),
    );

    await waitFor(() =>
      expect(mockApi.put).toHaveBeenCalledWith("/api/users/me/password", {
        body: JSON.stringify({
          current_password: "OldPass123!",
          new_password: "NewPass123!",
        }),
      }),
    );
    expect(
      await screen.findByText(t("account.password.changed")),
    ).toBeInTheDocument();
  });

  it("surfaces a password-policy reason inline", async () => {
    mockApi.put.mockRejectedValue(new ApiError(422, { reason: "too_short" }));
    renderPage();
    await waitForLoaded();

    fireEvent.change(screen.getByLabelText(t("account.password.current")), {
      target: { value: "OldPass123!" },
    });
    fireEvent.change(screen.getByLabelText(t("account.password.new")), {
      target: { value: "short" },
    });
    fireEvent.change(screen.getByLabelText(t("account.password.confirm")), {
      target: { value: "short" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("account.password.change") }),
    );

    expect(
      await screen.findByText(t("account.error.too_short")),
    ).toBeInTheDocument();
  });

  // Password managers match a saved credential to a change-password form by
  // its username field, and save a generated password against it, so the form
  // carries the signed-in username in a field that is present but out of
  // sight, out of the tab order and out of the accessibility tree (#4093,
  // #4094).
  it("carries the signed-in username in a hidden read-only field of the password form", async () => {
    renderPage();
    await waitForLoaded();

    const form = (
      screen.getByLabelText(t("account.password.current")) as HTMLInputElement
    ).form;
    const username = form?.querySelector('input[autocomplete="username"]');
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
  it("posts the change-password form", async () => {
    renderPage();
    await waitForLoaded();

    const current = screen.getByLabelText(
      t("account.password.current"),
    ) as HTMLInputElement;
    expect(current.form).toHaveAttribute("method", "post");
  });

  it("names each password input next to its autocomplete hint", async () => {
    renderPage();
    await waitForLoaded();

    const fields = [
      ["account.password.current", "current-password", "current-password"],
      ["account.password.new", "new-password", "new-password"],
      ["account.password.confirm", "confirm-password", "new-password"],
    ] as const;
    for (const [label, name, autocomplete] of fields) {
      const input = screen.getByLabelText(t(label));
      expect(input).toHaveAttribute("name", name);
      expect(input).toHaveAttribute("autocomplete", autocomplete);
    }
  });

  it("keeps the name and autocomplete when the show/hide toggle reveals a password", async () => {
    renderPage();
    await waitForLoaded();

    const input = screen.getByLabelText(t("account.password.new"));
    fireEvent.click(
      within(input.parentElement as HTMLElement).getByRole("button"),
    );

    const revealed = screen.getByLabelText(t("account.password.new"));
    expect(revealed).toHaveAttribute("type", "text");
    expect(revealed).toHaveAttribute("name", "new-password");
    expect(revealed).toHaveAttribute("autocomplete", "new-password");
  });
});

describe("AccountPage deletion", () => {
  it("gates deletion behind both the typed confirm and a password, then hard-logs-out", async () => {
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await waitForLoaded();

    fireEvent.click(
      screen.getByRole("button", { name: t("account.delete.open") }),
    );

    const confirmButton = screen.getByRole("button", {
      name: t("account.delete.confirm"),
    });
    expect(confirmButton).toBeDisabled();

    // The username alone is not enough: the password is still required.
    fireEvent.change(screen.getByLabelText(t("account.delete.prompt")), {
      target: { value: "miura" },
    });
    expect(confirmButton).toBeDisabled();

    // Both gates satisfied enables the destructive button.
    fireEvent.change(screen.getByLabelText(t("account.delete.password")), {
      target: { value: "MyPass123!" },
    });
    expect(confirmButton).toBeEnabled();

    fireEvent.click(confirmButton);

    await waitFor(() =>
      expect(mockApi.delete).toHaveBeenCalledWith("/api/users/me", {
        body: JSON.stringify({ password: "MyPass123!" }),
      }),
    );
    await waitFor(() => expect(mockLogout).toHaveBeenCalledTimes(1));
  });

  // The delete dialog's password field sits in a form with the signed-in
  // username, for password managers to offer the saved credential (#4121).
  it("carries the signed-in username in the delete dialog's password form", async () => {
    renderPage();
    await waitForLoaded();

    fireEvent.click(
      screen.getByRole("button", { name: t("account.delete.open") }),
    );

    const form = (
      screen.getByLabelText(t("account.delete.password")) as HTMLInputElement
    ).form;
    expect(form).not.toBeNull();
    const username = form?.querySelector('input[autocomplete="username"]');
    expect(username).toHaveValue("miura");
    expect(username).toHaveAttribute("readonly");
  });

  it("keeps deletion gated when only the password is entered", async () => {
    renderPage();
    await waitForLoaded();

    fireEvent.click(
      screen.getByRole("button", { name: t("account.delete.open") }),
    );
    fireEvent.change(screen.getByLabelText(t("account.delete.password")), {
      target: { value: "MyPass123!" },
    });

    expect(
      screen.getByRole("button", { name: t("account.delete.confirm") }),
    ).toBeDisabled();
  });

  it("surfaces a wrong-password 401 via toast and does not log out", async () => {
    mockApi.delete.mockRejectedValue(
      new ApiError(401, { reason: "invalid_credentials" }),
    );
    renderPage();
    await waitForLoaded();

    fireEvent.click(
      screen.getByRole("button", { name: t("account.delete.open") }),
    );
    fireEvent.change(screen.getByLabelText(t("account.delete.prompt")), {
      target: { value: "miura" },
    });
    fireEvent.change(screen.getByLabelText(t("account.delete.password")), {
      target: { value: "wrong" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("account.delete.confirm") }),
    );

    expect(
      await screen.findByText(t("account.error.invalid_credentials")),
    ).toBeInTheDocument();
    expect(mockLogout).not.toHaveBeenCalled();
  });

  it("surfaces an owns_community conflict via toast and does not log out", async () => {
    mockApi.delete.mockRejectedValue(
      new ApiError(409, { reason: "owns_community" }),
    );
    renderPage();
    await waitForLoaded();

    fireEvent.click(
      screen.getByRole("button", { name: t("account.delete.open") }),
    );
    fireEvent.change(screen.getByLabelText(t("account.delete.prompt")), {
      target: { value: "miura" },
    });
    fireEvent.change(screen.getByLabelText(t("account.delete.password")), {
      target: { value: "MyPass123!" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("account.delete.confirm") }),
    );

    expect(
      await screen.findByText(t("account.error.owns_community")),
    ).toBeInTheDocument();
    expect(mockLogout).not.toHaveBeenCalled();
  });
});

describe("AccountPage logout", () => {
  it("logs out via the session layer", async () => {
    renderPage();
    await waitForLoaded();

    fireEvent.click(screen.getByRole("button", { name: t("account.signOut") }));
    expect(mockLogout).toHaveBeenCalledTimes(1);
  });
});
