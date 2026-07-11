// @vitest-environment jsdom
// Pinned to jsdom: getByLabelText("Current password") is unambiguous under
// jsdom but matches both the input and the show/hide toggle under happy-dom
// (issue #1751).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
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

async function waitForLoaded() {
  await waitFor(() =>
    expect(screen.getByDisplayValue("miura")).toBeInTheDocument(),
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
        return Promise.reject(new ApiError(500, { reason: "server_error" }));
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

  it("keeps rendering the cached profile when a background refetch fails (#1797)", async () => {
    const { queryClient } = renderPage();
    await waitForLoaded();

    // Simulate a transient API outage: the next background refetch fails.
    mockApi.get.mockRejectedValue(
      new ApiError(500, { reason: "server_error" }),
    );
    await act(() => queryClient.invalidateQueries());
    // The query-state notification lands a task after invalidateQueries
    // settles; flush it so the assertion sees the post-refetch render.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The cached page stays on screen instead of the full-page error.
    expect(screen.getByDisplayValue("miura")).toBeInTheDocument();
    expect(screen.queryByText(t("account.loadError"))).not.toBeInTheDocument();
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

describe("AccountPage memberships refetch failure (#1805)", () => {
  it("keeps rendering cached communities when a background refetch fails", async () => {
    const { queryClient } = renderPage();
    await waitForLoaded();
    await screen.findByText("Sakura SMP");

    // Simulate a transient API outage: the next background refetch fails.
    mockApi.get.mockRejectedValue(
      new ApiError(500, { reason: "server_error" }),
    );
    await act(() => queryClient.invalidateQueries());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The cached communities list stays on screen instead of the error.
    expect(screen.getByText("Sakura SMP")).toBeInTheDocument();
    expect(
      screen.queryByText(t("account.memberships.loadError")),
    ).not.toBeInTheDocument();
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
