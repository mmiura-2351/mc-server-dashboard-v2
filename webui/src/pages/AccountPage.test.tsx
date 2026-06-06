import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <AccountPage />
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockApi.get.mockReset();
  mockApi.patch.mockReset();
  mockApi.put.mockReset();
  mockApi.delete.mockReset();
  mockLogout.mockReset();
  // Default: /users/me then /communities.
  mockApi.get.mockImplementation((path: string) => {
    if (path === "/users/me") return Promise.resolve(me);
    if (path === "/communities")
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
      expect(mockApi.patch).toHaveBeenCalledWith("/users/me", {
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
      expect(mockApi.put).toHaveBeenCalledWith("/users/me/password", {
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
  it("gates deletion behind typed confirm, then hard-logs-out on success", async () => {
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

    // Typing the username enables the destructive button.
    fireEvent.change(screen.getByLabelText(t("account.delete.prompt")), {
      target: { value: "miura" },
    });
    expect(confirmButton).toBeEnabled();

    fireEvent.click(confirmButton);

    await waitFor(() =>
      expect(mockApi.delete).toHaveBeenCalledWith("/users/me"),
    );
    await waitFor(() => expect(mockLogout).toHaveBeenCalledTimes(1));
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
