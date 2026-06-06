import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import { AdminUsersPage } from "./AdminUsersPage.tsx";

// Admin Users page tests (#475). The api client is mocked and dispatched by
// path; useCurrentUser is mocked so a test controls who "you" is (self-action
// edges). Each lifecycle action asserts the exact request shape the platform-
// admin routes declare (admin_users.py / schema.ts).

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return { ...actual, api: mockApi };
});

let mockMe: { id: string } = { id: "self" };
vi.mock("../auth/useCurrentUser.ts", () => ({
  useCurrentUser: () => ({ data: mockMe }),
}));

function user(over: Record<string, unknown> = {}) {
  return {
    id: "u1",
    username: "alice",
    email: "alice@example.com",
    is_platform_admin: false,
    active: true,
    created_at: "2024-01-02T00:00:00Z",
    ...over,
  };
}

function listResponse(over: Record<string, unknown> = {}) {
  return {
    users: [user()],
    total: 1,
    limit: 50,
    offset: 0,
    ...over,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>
          <AdminUsersPage />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockMe = { id: "self" };
  mockApi.get.mockReset();
  mockApi.post.mockReset();
  mockApi.put.mockReset();
  mockApi.delete.mockReset();
  mockApi.get.mockResolvedValue(listResponse());
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("AdminUsersPage", () => {
  it("lists users with count and request limit/offset", async () => {
    renderPage();
    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("alice@example.com")).toBeInTheDocument();
    expect(mockApi.get).toHaveBeenCalledWith("/users?limit=50&offset=0");
  });

  it("pages forward with a new offset when there are more users", async () => {
    mockApi.get.mockResolvedValue(
      listResponse({ total: 120, users: [user()] }),
    );
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.next") }),
    );

    await waitFor(() => {
      expect(mockApi.get).toHaveBeenCalledWith("/users?limit=50&offset=50");
    });
  });

  it("deactivates an active user via POST .../deactivate", async () => {
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.deactivate") }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith("/users/u1/deactivate");
    });
  });

  it("reactivates a deactivated user via POST .../reactivate", async () => {
    mockApi.get.mockResolvedValue(
      listResponse({ users: [user({ active: false })] }),
    );
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.reactivate") }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith("/users/u1/reactivate");
    });
  });

  it("grants platform admin via PUT with grant: true", async () => {
    mockApi.put.mockResolvedValue(undefined);
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.makeAdmin") }),
    );

    await waitFor(() => {
      expect(mockApi.put).toHaveBeenCalledWith("/users/u1/platform-admin", {
        body: JSON.stringify({ grant: true }),
      });
    });
  });

  it("revokes another admin's flag directly via PUT with grant: false", async () => {
    mockApi.get.mockResolvedValue(
      listResponse({ users: [user({ is_platform_admin: true })] }),
    );
    mockApi.put.mockResolvedValue(undefined);
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.revokeAdmin") }),
    );

    await waitFor(() => {
      expect(mockApi.put).toHaveBeenCalledWith("/users/u1/platform-admin", {
        body: JSON.stringify({ grant: false }),
      });
    });
  });

  it("confirms before revoking your own admin flag (API allows it)", async () => {
    mockMe = { id: "u1" };
    mockApi.get.mockResolvedValue(
      listResponse({ users: [user({ is_platform_admin: true })] }),
    );
    mockApi.put.mockResolvedValue(undefined);
    renderPage();
    await screen.findByText("alice");

    // Revoke is offered for self; deactivate/delete are not.
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.revokeAdmin") }),
    );
    expect(
      screen.queryByRole("button", { name: t("admin.users.deactivate") }),
    ).not.toBeInTheDocument();

    // No PUT until the self-revoke confirm.
    expect(mockApi.put).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.selfRevokeConfirm") }),
    );

    await waitFor(() => {
      expect(mockApi.put).toHaveBeenCalledWith("/users/u1/platform-admin", {
        body: JSON.stringify({ grant: false }),
      });
    });
  });

  it("does not offer deactivate or delete for your own row", async () => {
    mockMe = { id: "u1" };
    renderPage();
    await screen.findByText("alice");

    expect(
      screen.queryByRole("button", { name: t("admin.users.deactivate") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("admin.users.delete") }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(t("admin.users.you"))).toBeInTheDocument();
  });

  it("deletes a user only after the typed username confirm", async () => {
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.delete") }),
    );

    const confirmBtn = screen.getByRole("button", {
      name: t("admin.users.deleteConfirm"),
    });
    expect(confirmBtn).toBeDisabled();
    expect(mockApi.delete).not.toHaveBeenCalled();

    fireEvent.change(screen.getByPlaceholderText("alice"), {
      target: { value: "alice" },
    });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith("/users/u1");
    });
  });

  it("disables the row actions while a lifecycle mutation is in flight", async () => {
    // Hold the deactivate request open so the in-flight guard is observable.
    let resolve: (() => void) | undefined;
    mockApi.post.mockImplementation(
      () =>
        new Promise<undefined>((r) => {
          resolve = () => r(undefined);
        }),
    );
    renderPage();
    await screen.findByText("alice");

    const deactivate = screen.getByRole("button", {
      name: t("admin.users.deactivate"),
    });
    fireEvent.click(deactivate);

    // The row's actions disable while the mutation is pending so a second click
    // can't double-fire.
    await waitFor(() => {
      expect(deactivate).toBeDisabled();
    });
    expect(
      screen.getByRole("button", { name: t("admin.users.delete") }),
    ).toBeDisabled();
    fireEvent.click(deactivate);
    expect(mockApi.post).toHaveBeenCalledTimes(1);

    resolve?.();
    await waitFor(() => {
      expect(deactivate).not.toBeDisabled();
    });
  });

  it("surfaces the self_target conflict message on a 409", async () => {
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "self_target" }),
    );
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.deactivate") }),
    );

    expect(
      await screen.findByText(t("admin.users.error.self_target")),
    ).toBeInTheDocument();
  });

  it("creates a user via POST /admin/users", async () => {
    mockApi.post.mockResolvedValue({
      id: "n1",
      username: "bob",
      email: "bob@example.com",
      is_platform_admin: false,
    });
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("admin.users.usernameLabel")), {
      target: { value: "bob" },
    });
    fireEvent.change(screen.getByLabelText(t("admin.users.emailLabel")), {
      target: { value: "bob@example.com" },
    });
    fireEvent.change(screen.getByLabelText(t("admin.users.passwordLabel")), {
      target: { value: "Sup3rSecret!pw" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.createSubmit") }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith("/admin/users", {
        body: JSON.stringify({
          username: "bob",
          email: "bob@example.com",
          password: "Sup3rSecret!pw",
        }),
      });
    });
  });

  it("shows a password-policy 422 reason inline on the password field", async () => {
    mockApi.post.mockRejectedValue(new ApiError(422, { reason: "too_short" }));
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("admin.users.usernameLabel")), {
      target: { value: "bob" },
    });
    fireEvent.change(screen.getByLabelText(t("admin.users.emailLabel")), {
      target: { value: "bob@example.com" },
    });
    fireEvent.change(screen.getByLabelText(t("admin.users.passwordLabel")), {
      target: { value: "short" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.createSubmit") }),
    );

    expect(
      await screen.findByText(t("register.reason.too_short")),
    ).toBeInTheDocument();
  });

  it("shows a 409 username conflict inline on the username field", async () => {
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "username_taken" }),
    );
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("admin.users.usernameLabel")), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText(t("admin.users.emailLabel")), {
      target: { value: "new@example.com" },
    });
    fireEvent.change(screen.getByLabelText(t("admin.users.passwordLabel")), {
      target: { value: "Sup3rSecret!pw" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.createSubmit") }),
    );

    expect(
      await screen.findByText(t("register.reason.username_taken")),
    ).toBeInTheDocument();
  });

  it("shows a generic inline error when create fails without a mapped reason", async () => {
    // A 500 / network failure carries no problem reason; the dialog must still
    // surface feedback rather than sit silent.
    mockApi.post.mockRejectedValue(new Error("network down"));
    renderPage();
    await screen.findByText("alice");

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("admin.users.usernameLabel")), {
      target: { value: "bob" },
    });
    fireEvent.change(screen.getByLabelText(t("admin.users.emailLabel")), {
      target: { value: "bob@example.com" },
    });
    fireEvent.change(screen.getByLabelText(t("admin.users.passwordLabel")), {
      target: { value: "Sup3rSecret!pw" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.users.createSubmit") }),
    );

    expect(
      await screen.findByText(t("admin.users.error.generic")),
    ).toBeInTheDocument();
  });
});
