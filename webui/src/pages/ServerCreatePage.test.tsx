import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { ToastProvider } from "../components/Toast.tsx";
import {
  _resetForTesting as resetUploadProgress,
  useUploadProgress,
} from "../components/useUploadProgress.ts";
import { t } from "../i18n/index.ts";
import { meta } from "../test/meta.ts";
import { ServerCreatePage } from "./ServerCreatePage.tsx";

const CID = "c1";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

const mockPostFormWithProgress = vi.hoisted(() => vi.fn());

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return {
    ...actual,
    api: mockApi,
    postFormWithProgress: mockPostFormWithProgress,
  };
});

let mockCanCreate = true;
vi.mock("../permissions/ActiveCommunityProvider.tsx", () => ({
  useActiveCommunity: () => ({
    communityId: CID,
    setCommunityId: vi.fn(),
    communities: [{ id: CID, name: "Sakura" }],
  }),
}));
vi.mock("../permissions/useCan.ts", () => ({
  useCanCode: () => mockCanCreate,
}));

function defaultGet(path: string) {
  if (path === "/api/versions") {
    return Promise.resolve({ server_types: ["vanilla", "paper", "fabric"] });
  }
  if (path.startsWith("/api/versions/")) {
    return Promise.resolve({ versions: ["1.21.6", "1.21.5"] });
  }
  if (path.startsWith("/api/ports/check/")) {
    return Promise.resolve({ port: 25565, in_range: true, available: true });
  }
  if (path === "/api/ports/available") {
    return Promise.resolve({ ports: [25570] });
  }
  if (path === "/api/meta") {
    // Include explicit null memory defaults so omission cannot make tests pass via fallback behavior.
    return Promise.resolve(meta());
  }
  return Promise.reject(new Error(`unexpected GET ${path}`));
}

let lastPath = "";
function LocationProbe() {
  const loc = useLocation();
  lastPath = loc.pathname;
  return null;
}

// Mount under the URL community because :cid selects the wizard's target.
function renderPage(path = `/communities/${CID}/servers/new`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <LocationProbe />
          <Routes>
            <Route
              path="/communities/:cid/servers/new"
              element={<ServerCreatePage />}
            />
          </Routes>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...result, queryClient };
}

function activeTab(): string | null {
  return (
    screen
      .getAllByRole("tab")
      .find((el) => el.getAttribute("aria-selected") === "true")?.textContent ??
    null
  );
}

// Wait until the latest-version effect enables Next; clicking before that races the wizard state.
async function pickTypeAndVersion() {
  fireEvent.click(await screen.findByText(t("serverCreate.type.paper")));
  await waitFor(() =>
    expect(screen.getByText(t("serverCreate.next"))).toBeEnabled(),
  );
}

async function reachConfigStep(name = "survival") {
  await pickTypeAndVersion();
  fireEvent.click(screen.getByText(t("serverCreate.next")));
  const nameInput = await screen.findByLabelText(t("serverCreate.nameLabel"));
  fireEvent.change(nameInput, { target: { value: name } });
}

beforeEach(() => {
  mockApi.get.mockReset();
  mockApi.post.mockReset();
  mockPostFormWithProgress.mockReset();
  lastPath = "";
  mockCanCreate = true;
  mockApi.get.mockImplementation(defaultGet);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("ServerCreatePage gating", () => {
  it("gates the whole page on server:create", () => {
    mockCanCreate = false;
    renderPage();
    expect(screen.getByText(t("serverCreate.denied"))).toBeInTheDocument();
    expect(screen.queryByText(t("serverCreate.typeHeading"))).toBeNull();
  });

  it("shows the not-found state for a URL cid outside the membership list (#784)", () => {
    renderPage("/communities/other/servers/new");
    expect(screen.getByText(t("community.notFound.title"))).toBeInTheDocument();
    expect(screen.queryByText(t("serverCreate.typeHeading"))).toBeNull();
  });
});

describe("Step 1 — type & version", () => {
  it("preselects the latest version after picking a type", async () => {
    renderPage();
    fireEvent.click(await screen.findByText(t("serverCreate.type.paper")));
    expect(await screen.findByDisplayValue("1.21.6")).toBeInTheDocument();
  });

  it("blocks Next until a type and version are chosen", async () => {
    renderPage();
    await screen.findByText(t("serverCreate.type.paper"));
    expect(screen.getByText(t("serverCreate.next"))).toBeDisabled();
    fireEvent.click(screen.getByText(t("serverCreate.type.paper")));
    // The select displays its first option before state commits; wait for Next to enable.
    await waitFor(() =>
      expect(screen.getByText(t("serverCreate.next"))).toBeEnabled(),
    );
  });

  it("falls back to the raw type string for a catalog type not in TYPE_LABEL (#791)", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/api/versions") {
        return Promise.resolve({
          server_types: ["vanilla", "paper", "future_engine"],
        });
      }
      return defaultGet(path);
    });
    renderPage();

    expect(
      await screen.findByText(t("serverCreate.type.vanilla")),
    ).toBeInTheDocument();
    expect(screen.getByText("future_engine")).toBeInTheDocument();
  });
});

describe("Step 2 — port check", () => {
  it("auto-suggests the game port from /ports/available on entering step 2", async () => {
    renderPage();
    await pickTypeAndVersion();
    fireEvent.click(screen.getByText(t("serverCreate.next")));

    expect(await screen.findByDisplayValue("25570")).toBeInTheDocument();
    expect(mockApi.get).toHaveBeenCalledWith("/api/ports/available");
  });

  it("does not clobber a user-typed port with the suggestion", async () => {
    let resolveAvailable: (v: unknown) => void = () => {};
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/api/ports/available") {
        return new Promise((resolve) => {
          resolveAvailable = resolve;
        });
      }
      return defaultGet(path);
    });
    renderPage();
    await pickTypeAndVersion();
    fireEvent.click(screen.getByText(t("serverCreate.next")));

    const portInput = await screen.findByLabelText(t("serverCreate.portLabel"));
    fireEvent.change(portInput, { target: { value: "30000" } });
    resolveAvailable({ ports: [25570] });

    await waitFor(() =>
      expect(screen.getByLabelText(t("serverCreate.portLabel"))).toHaveValue(
        30000,
      ),
    );
  });

  it("reports an available port on blur", async () => {
    renderPage();
    await pickTypeAndVersion();
    fireEvent.click(screen.getByText(t("serverCreate.next")));

    const portInput = await screen.findByLabelText(t("serverCreate.portLabel"));
    fireEvent.change(portInput, { target: { value: "25565" } });
    fireEvent.blur(portInput);

    expect(
      await screen.findByText(t("serverCreate.portAvailable")),
    ).toBeInTheDocument();
  });

  it("flags a taken port on blur", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.startsWith("/api/ports/check/")) {
        return Promise.resolve({ in_range: true, available: false });
      }
      return defaultGet(path);
    });
    renderPage();
    await pickTypeAndVersion();
    fireEvent.click(screen.getByText(t("serverCreate.next")));

    const portInput = await screen.findByLabelText(t("serverCreate.portLabel"));
    fireEvent.change(portInput, { target: { value: "25565" } });
    fireEvent.blur(portInput);

    expect(
      await screen.findByText(t("serverCreate.portTaken")),
    ).toBeInTheDocument();
  });

  it("ignores a stale out-of-order port-check response (#1592)", async () => {
    // Resolve port checks out of order to verify the stale verdict cannot replace the current one.
    let resolveFirst: (v: unknown) => void = () => {};
    let resolveSecond: (v: unknown) => void = () => {};
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/api/ports/check/25565") {
        return new Promise((resolve) => {
          resolveFirst = resolve;
        });
      }
      if (path === "/api/ports/check/25566") {
        return new Promise((resolve) => {
          resolveSecond = resolve;
        });
      }
      return defaultGet(path);
    });
    renderPage();
    await pickTypeAndVersion();
    fireEvent.click(screen.getByText(t("serverCreate.next")));

    const portInput = await screen.findByLabelText(t("serverCreate.portLabel"));
    fireEvent.change(portInput, { target: { value: "25565" } });
    fireEvent.blur(portInput);
    fireEvent.change(portInput, { target: { value: "25566" } });
    fireEvent.blur(portInput);

    resolveSecond({ in_range: true, available: true });
    expect(
      await screen.findByText(t("serverCreate.portAvailable")),
    ).toBeInTheDocument();

    resolveFirst({ in_range: true, available: false });
    await waitFor(() =>
      expect(
        screen.getByText(t("serverCreate.portAvailable")),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(t("serverCreate.portTaken"))).toBeNull();
  });
});

describe("Step 2 — port control gated on relay mode (#1002)", () => {
  function relayGet(path: string) {
    if (path === "/api/meta") {
      return Promise.resolve(meta({ relay_enabled: true }));
    }
    return defaultGet(path);
  }

  it("shows the port control in direct mode (relay off)", async () => {
    renderPage();
    await pickTypeAndVersion();
    fireEvent.click(screen.getByText(t("serverCreate.next")));
    expect(
      await screen.findByLabelText(t("serverCreate.portLabel")),
    ).toBeInTheDocument();
  });

  it("shows the port control while /api/meta is still loading (#1061)", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/api/meta") {
        return new Promise(() => {});
      }
      return defaultGet(path);
    });
    renderPage();
    await pickTypeAndVersion();
    fireEvent.click(screen.getByText(t("serverCreate.next")));
    expect(
      await screen.findByLabelText(t("serverCreate.portLabel")),
    ).toBeInTheDocument();
  });

  it("shows the port control when /api/meta fails (#1061)", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/api/meta") {
        return Promise.reject(new Error("network error"));
      }
      return defaultGet(path);
    });
    renderPage();
    await pickTypeAndVersion();
    fireEvent.click(screen.getByText(t("serverCreate.next")));
    await waitFor(() =>
      expect(
        screen.getByLabelText(t("serverCreate.portLabel")),
      ).toBeInTheDocument(),
    );
  });

  it("hides the port control in relay mode and never queries /ports/available", async () => {
    mockApi.get.mockImplementation(relayGet);
    renderPage();
    await pickTypeAndVersion();
    fireEvent.click(screen.getByText(t("serverCreate.next")));
    await waitFor(() =>
      expect(screen.queryByLabelText(t("serverCreate.portLabel"))).toBeNull(),
    );
    expect(mockApi.get).not.toHaveBeenCalledWith("/api/ports/available");
  });

  it("omits game_port from the create body in relay mode", async () => {
    mockApi.get.mockImplementation(relayGet);
    mockApi.post.mockResolvedValue({ id: "s-new" });
    renderPage();
    await reachConfigStep();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );
    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const body = JSON.parse(mockApi.post.mock.calls[0][1].body);
    expect(body.game_port).toBeUndefined();
  });
});

describe("Step 2 — config & EULA", () => {
  it("warns when the EULA is not accepted", async () => {
    renderPage();
    await reachConfigStep();
    expect(screen.getByText(t("serverCreate.eulaWarning"))).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(t("serverCreate.eulaLabel")));
    expect(screen.queryByText(t("serverCreate.eulaWarning"))).toBeNull();
  });

  it("creates and navigates to the new server detail route", async () => {
    mockApi.post.mockResolvedValue({ id: "s-new" });
    renderPage();
    await reachConfigStep();
    fireEvent.click(screen.getByLabelText(t("serverCreate.eulaLabel")));
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );

    await waitFor(() => {
      expect(lastPath).toBe(`/communities/${CID}/servers/s-new`);
    });
    const body = JSON.parse(mockApi.post.mock.calls[0][1].body);
    expect(body).toMatchObject({
      name: "survival",
      mc_edition: "java",
      mc_version: "1.21.6",
      server_type: "paper",
      accept_eula: true,
    });
  });

  it("sends server.properties overrides as config", async () => {
    mockApi.post.mockResolvedValue({ id: "s-new" });
    renderPage();
    await reachConfigStep();
    fireEvent.click(screen.getByText(t("serverCreate.propAdd")));
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.propKeyPlaceholder")),
      {
        target: { value: "motd" },
      },
    );
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.propValuePlaceholder")),
      { target: { value: "hello" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const body = JSON.parse(mockApi.post.mock.calls[0][1].body);
    expect(body.config).toEqual({ motd: "hello" });
  });
});

describe("Step 2 — resource allocation (#715)", () => {
  it("includes set memory limit and CPU allocation in the create config", async () => {
    mockApi.post.mockResolvedValue({ id: "s-new" });
    renderPage();
    await reachConfigStep();
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.memoryLimitLabel")),
      { target: { value: "2048" } },
    );
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.cpuAllocationLabel")),
      { target: { value: "2000" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const body = JSON.parse(mockApi.post.mock.calls[0][1].body);
    expect(body.config).toEqual({ memory_limit_mb: 2048, cpu_millis: 2000 });
  });

  it("omits blank resource fields from the create config", async () => {
    mockApi.post.mockResolvedValue({ id: "s-new" });
    renderPage();
    await reachConfigStep();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const body = JSON.parse(mockApi.post.mock.calls[0][1].body);
    expect(body.config).toEqual({});
  });

  it("prefills the memory limit with the operator default from /meta (#1069)", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/api/meta") {
        return Promise.resolve(meta({ default_memory_limit_mb: 2048 }));
      }
      return defaultGet(path);
    });
    renderPage();
    await reachConfigStep();
    const input = (await screen.findByLabelText(
      t("serverCreate.memoryLimitLabel"),
    )) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("2048"));
  });

  it("blocks submit on a memory limit below the floor", async () => {
    renderPage();
    await reachConfigStep();
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.memoryLimitLabel")),
      { target: { value: "256" } },
    );
    expect(
      screen.getByText(t("serverCreate.memoryLimitRange")),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    ).toBeDisabled();
  });

  it("blocks submit on a memory limit above the ceiling", async () => {
    renderPage();
    await reachConfigStep();
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.memoryLimitLabel")),
      { target: { value: "1048577" } },
    );
    expect(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    ).toBeDisabled();
  });

  it("blocks submit above the operator-configured ceiling that the default would allow (#1069)", async () => {
    // Choose a value below the built-in ceiling but above /meta's cap to verify the operator limit is used.
    mockApi.get.mockImplementation((path: string) => {
      if (path === "/api/meta") {
        return Promise.resolve(meta({ max_memory_limit_mb: 4096 }));
      }
      return defaultGet(path);
    });
    renderPage();
    await reachConfigStep();
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.memoryLimitLabel")),
      { target: { value: "8192" } },
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: t("serverCreate.create") }),
      ).toBeDisabled(),
    );
    expect(
      screen.getByText(t("serverCreate.memoryLimitRange")),
    ).toBeInTheDocument();
  });

  it("blocks submit on a non-integer memory limit", async () => {
    renderPage();
    await reachConfigStep();
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.memoryLimitLabel")),
      { target: { value: "1024.5" } },
    );
    expect(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    ).toBeDisabled();
  });

  it("blocks submit on a CPU allocation below the floor", async () => {
    renderPage();
    await reachConfigStep();
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.cpuAllocationLabel")),
      { target: { value: "50" } },
    );
    expect(
      screen.getByText(t("serverCreate.cpuAllocationRange")),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    ).toBeDisabled();
  });

  it("blocks submit on a CPU allocation above the ceiling", async () => {
    renderPage();
    await reachConfigStep();
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.cpuAllocationLabel")),
      { target: { value: "128001" } },
    );
    expect(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    ).toBeDisabled();
  });

  it("blocks submit on a non-integer CPU allocation", async () => {
    renderPage();
    await reachConfigStep();
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.cpuAllocationLabel")),
      { target: { value: "100.5" } },
    );
    expect(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    ).toBeDisabled();
  });
});

describe("create error surfacing", () => {
  it("toasts a mapped failure and keeps the form for a retry", async () => {
    mockApi.post.mockRejectedValue(new ApiError(409, { reason: "port_taken" }));
    renderPage();
    await reachConfigStep();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );
    expect(
      (await screen.findByText(t("serverCreate.error.port_taken"))).closest(
        ".toast",
      ),
    ).not.toBeNull();
    expect(screen.queryByText(t("serverCreate.genericError"))).toBeNull();
    expect(lastPath).toBe(`/communities/${CID}/servers/new`);
    expect(screen.getByLabelText(t("serverCreate.nameLabel"))).toHaveValue(
      "survival",
    );
    expect(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    ).toBeEnabled();
  });

  it("shows a name-field reason inline on the name field", async () => {
    mockApi.post.mockRejectedValue(
      new ApiError(422, { reason: "invalid_server_name" }),
    );
    renderPage();
    await reachConfigStep();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      t("serverCreate.error.invalid_server_name"),
    );
    expect(alert.closest(".field")).toContainElement(
      screen.getByLabelText(t("serverCreate.nameLabel")),
    );
    expect(document.querySelector(".toast")).toBeNull();
  });

  it("maps a structural validation_error on name to the field", async () => {
    mockApi.post.mockRejectedValue(
      new ApiError(422, {
        reason: "validation_error",
        errors: [{ loc: ["body", "name"], msg: "String too short" }],
      }),
    );
    renderPage();
    await reachConfigStep();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );
    expect(await screen.findByText("String too short")).toBeInTheDocument();
  });
});

describe("Step 2 — join address name (slug) field (issue #981, gated on relay #1006)", () => {
  function relayGet(path: string) {
    if (path === "/api/meta") {
      return Promise.resolve(meta({ relay_enabled: true }));
    }
    return defaultGet(path);
  }

  it("renders the optional slug field on step 2 in relay mode", async () => {
    mockApi.get.mockImplementation(relayGet);
    renderPage();
    await reachConfigStep();
    expect(
      screen.getByLabelText(t("serverCreate.slugLabel")),
    ).toBeInTheDocument();
    expect(screen.getByText(t("serverCreate.slugHint"))).toBeInTheDocument();
  });

  it("hides the slug field in direct mode (relay off)", async () => {
    renderPage();
    await reachConfigStep();
    expect(screen.queryByLabelText(t("serverCreate.slugLabel"))).toBeNull();
  });

  it("sends the slug when explicitly set", async () => {
    mockApi.get.mockImplementation(relayGet);
    mockApi.post.mockResolvedValue({ id: "s-new" });
    renderPage();
    await reachConfigStep();
    fireEvent.change(screen.getByLabelText(t("serverCreate.slugLabel")), {
      target: { value: "myslug" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );
    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const body = JSON.parse(mockApi.post.mock.calls[0][1].body);
    expect(body.slug).toBe("myslug");
  });

  it("omits slug from the body when the field is left blank", async () => {
    mockApi.get.mockImplementation(relayGet);
    mockApi.post.mockResolvedValue({ id: "s-new" });
    renderPage();
    await reachConfigStep();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );
    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const body = JSON.parse(mockApi.post.mock.calls[0][1].body);
    expect(body.slug).toBeUndefined();
  });

  it("shows inline error for an invalid slug", async () => {
    mockApi.get.mockImplementation(relayGet);
    renderPage();
    await reachConfigStep();
    fireEvent.change(screen.getByLabelText(t("serverCreate.slugLabel")), {
      target: { value: "INVALID-UPPER" },
    });
    expect(screen.getByText(t("serverCreate.slugInvalid"))).toBeInTheDocument();
  });

  it("blocks submit while the slug is invalid", async () => {
    mockApi.get.mockImplementation(relayGet);
    renderPage();
    await reachConfigStep();
    fireEvent.change(screen.getByLabelText(t("serverCreate.slugLabel")), {
      target: { value: "-bad-start" },
    });
    expect(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    ).toBeDisabled();
  });

  it("surfaces 409 slug_taken inline on the slug field", async () => {
    mockApi.get.mockImplementation(relayGet);
    mockApi.post.mockRejectedValue(new ApiError(409, { reason: "slug_taken" }));
    renderPage();
    await reachConfigStep();
    fireEvent.change(screen.getByLabelText(t("serverCreate.slugLabel")), {
      target: { value: "taken" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(t("serverCreate.error.slug_taken"));
    const slugInput = screen.getByLabelText(t("serverCreate.slugLabel"));
    expect(alert.closest(".field")).toContainElement(slugInput);
    expect(slugInput).toHaveValue("taken");
    expect(lastPath).toBe(`/communities/${CID}/servers/new`);
  });

  it("clears the API slug error when the user edits the field", async () => {
    mockApi.get.mockImplementation(relayGet);
    mockApi.post.mockRejectedValue(new ApiError(409, { reason: "slug_taken" }));
    renderPage();
    await reachConfigStep();
    fireEvent.change(screen.getByLabelText(t("serverCreate.slugLabel")), {
      target: { value: "taken" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );
    await screen.findByText(t("serverCreate.error.slug_taken"));

    fireEvent.change(screen.getByLabelText(t("serverCreate.slugLabel")), {
      target: { value: "different" },
    });
    expect(screen.queryByText(t("serverCreate.error.slug_taken"))).toBeNull();
  });
});

describe("import tab", () => {
  it("imports a ZIP and navigates to the new server", async () => {
    mockPostFormWithProgress.mockResolvedValue({ id: "s-imported" });
    renderPage();
    fireEvent.click(await screen.findByText(t("serverCreate.tab.import")));

    fireEvent.change(
      await screen.findByLabelText(t("serverCreate.nameLabel")),
      { target: { value: "restored" } },
    );
    const file = new File(["zip-bytes"], "export.zip", {
      type: "application/zip",
    });
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.import.fileLabel")),
      {
        target: { files: [file] },
      },
    );
    fireEvent.click(screen.getByText(t("serverCreate.import.submit")));

    await waitFor(() => {
      expect(lastPath).toBe(`/communities/${CID}/servers/s-imported`);
    });
    const form = mockPostFormWithProgress.mock.calls[0][1] as FormData;
    expect(form.get("name")).toBe("restored");
    expect(form.get("file")).toBeInstanceOf(File);
  });

  it("resets upload progress on successful import (#1984)", async () => {
    resetUploadProgress();
    mockPostFormWithProgress.mockResolvedValue({ id: "s-imported" });
    renderPage();
    fireEvent.click(await screen.findByText(t("serverCreate.tab.import")));

    fireEvent.change(
      await screen.findByLabelText(t("serverCreate.nameLabel")),
      { target: { value: "restored" } },
    );
    const file = new File(["zip-bytes"], "export.zip", {
      type: "application/zip",
    });
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.import.fileLabel")),
      { target: { files: [file] } },
    );
    fireEvent.click(screen.getByText(t("serverCreate.import.submit")));

    await waitFor(() => {
      expect(lastPath).toBe(`/communities/${CID}/servers/s-imported`);
    });

    const { result } = renderHook(() => useUploadProgress());
    expect(result.current.active).toBe(false);
  });

  it("shows the chosen filename after selecting a file", async () => {
    renderPage();
    fireEvent.click(await screen.findByText(t("serverCreate.tab.import")));
    expect(
      await screen.findByText(t("common.noFileChosen")),
    ).toBeInTheDocument();

    const file = new File(["zip-bytes"], "export.zip", {
      type: "application/zip",
    });
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.import.fileLabel")),
      {
        target: { files: [file] },
      },
    );

    expect(await screen.findByText("export.zip")).toBeInTheDocument();
    expect(screen.queryByText(t("common.noFileChosen"))).toBeNull();
  });

  async function submitImport() {
    renderPage();
    fireEvent.click(await screen.findByText(t("serverCreate.tab.import")));
    fireEvent.change(
      await screen.findByLabelText(t("serverCreate.nameLabel")),
      { target: { value: "restored" } },
    );
    const file = new File(["zip"], "export.zip");
    fireEvent.change(
      screen.getByLabelText(t("serverCreate.import.fileLabel")),
      {
        target: { files: [file] },
      },
    );
    fireEvent.click(screen.getByText(t("serverCreate.import.submit")));
  }

  it("surfaces a 409 slug_taken on import instead of failing silently (#3022)", async () => {
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(409, { reason: "slug_taken" }),
    );
    await submitImport();

    expect(
      (
        await screen.findByText(t("serverCreate.import.error.slug_taken"))
      ).closest(".toast"),
    ).not.toBeNull();
    expect(
      screen.queryByText(t("serverCreate.genericError")),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(t("serverCreate.error.slug_taken")),
    ).not.toBeInTheDocument();
    expect(lastPath).toBe(`/communities/${CID}/servers/new`);
  });

  it("shows a name-field reason inline on the import name field", async () => {
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(422, { reason: "invalid_server_name" }),
    );
    await submitImport();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      t("serverCreate.error.invalid_server_name"),
    );
    expect(alert.closest(".field")).toContainElement(
      screen.getByLabelText(t("serverCreate.nameLabel")),
    );
    expect(document.querySelector(".toast")).toBeNull();
  });
});

describe("ServerCreatePage import deep link (#540)", () => {
  it("deep-links to the import tab via the #import hash", async () => {
    renderPage(`/communities/${CID}/servers/new#import`);
    expect(
      await screen.findByText(t("serverCreate.import.heading")),
    ).toBeInTheDocument();
    expect(activeTab()).toBe(t("serverCreate.tab.import"));
  });
});
