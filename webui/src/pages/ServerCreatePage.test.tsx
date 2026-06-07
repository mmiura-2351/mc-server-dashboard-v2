import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import { ServerCreatePage } from "./ServerCreatePage.tsx";

const CID = "c1";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  postForm: vi.fn(),
}));

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return { ...actual, api: mockApi };
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

// Route api.get by path so the wizard's parallel catalog/version/port reads each
// resolve to their endpoint's shape.
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
  return Promise.reject(new Error(`unexpected GET ${path}`));
}

// A location probe: mirrors the current path + hash so a test can assert the
// post-create redirect (the URL the page navigates to) and the tab hash that
// useTabHash writes — both go through real router navigation now that
// useNavigate is no longer mocked.
let lastPath = "";
let lastHash = "";
function LocationProbe() {
  const loc = useLocation();
  lastPath = loc.pathname;
  lastHash = loc.hash;
  return null;
}

function renderPage(path = `/communities/${CID}/servers/new`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <LocationProbe />
          <ServerCreatePage />
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function activeTab(): string | null {
  return (
    screen
      .getAllByRole("tab")
      .find((el) => el.getAttribute("aria-selected") === "true")?.textContent ??
    null
  );
}

// Pick the Paper type and wait for the latest-version preselect effect to
// commit the version into state — until then Next stays disabled and a click on
// it is a no-op, which races the rest of the walk under full-suite timing.
async function pickTypeAndVersion() {
  fireEvent.click(await screen.findByText(t("serverCreate.type.paper")));
  await waitFor(() =>
    expect(screen.getByText(t("serverCreate.next"))).toBeEnabled(),
  );
}

// Walk steps 1→3 leaving a created-ready form: pick a type, the latest version
// preselects, advance to runtime then config, and fill the name.
async function reachConfigStep(name = "survival") {
  await pickTypeAndVersion();
  fireEvent.click(screen.getByText(t("serverCreate.next")));
  fireEvent.click(await screen.findByText(t("serverCreate.next")));
  const nameInput = await screen.findByLabelText(t("serverCreate.nameLabel"));
  fireEvent.change(nameInput, { target: { value: name } });
}

beforeEach(() => {
  mockApi.get.mockReset();
  mockApi.post.mockReset();
  mockApi.postForm.mockReset();
  lastPath = "";
  lastHash = "";
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
});

describe("Step 1 — type & version", () => {
  it("renders the catalog type cards plus a disabled spigot card", async () => {
    renderPage();
    expect(
      await screen.findByText(t("serverCreate.type.vanilla")),
    ).toBeInTheDocument();
    expect(screen.getByText(t("serverCreate.type.paper"))).toBeInTheDocument();
    const spigot = screen
      .getByText(t("serverCreate.type.spigot"))
      .closest("button");
    expect(spigot).toBeDisabled();
    expect(spigot).toHaveAttribute("title", t("serverCreate.spigotHint"));
  });

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
    // Next flips to enabled only once the preselect effect commits the version
    // into state — anchor on that, not on the select's displayed option (which
    // shows the first option before the effect runs and races the assertion).
    await waitFor(() =>
      expect(screen.getByText(t("serverCreate.next"))).toBeEnabled(),
    );
  });
});

describe("Step 2 — runtime port check", () => {
  it("auto-suggests the game port from /ports/available on entering step 2", async () => {
    renderPage();
    await pickTypeAndVersion();
    fireEvent.click(screen.getByText(t("serverCreate.next")));

    expect(await screen.findByDisplayValue("25570")).toBeInTheDocument();
    expect(mockApi.get).toHaveBeenCalledWith("/api/ports/available");
  });

  it("does not clobber a user-typed port with the suggestion", async () => {
    // Block the suggest until after the user types, then let it resolve: the
    // user's value must win.
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
});

describe("Step 3 — config & EULA", () => {
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

describe("create error surfacing", () => {
  it("surfaces a 409 port_taken specifically", async () => {
    mockApi.post.mockRejectedValue(new ApiError(409, { reason: "port_taken" }));
    renderPage();
    await reachConfigStep();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );
    expect(
      await screen.findByText(t("serverCreate.error.port_taken")),
    ).toBeInTheDocument();
    expect(lastPath).toBe(`/communities/${CID}/servers/new`);
  });

  it("surfaces 422 spigot_unsupported", async () => {
    mockApi.post.mockRejectedValue(
      new ApiError(422, { reason: "spigot_unsupported" }),
    );
    renderPage();
    await reachConfigStep();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverCreate.create") }),
    );
    expect(
      await screen.findByText(t("serverCreate.error.spigot_unsupported")),
    ).toBeInTheDocument();
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

describe("import tab", () => {
  it("imports a ZIP and navigates to the new server", async () => {
    mockApi.postForm.mockResolvedValue({ id: "s-imported" });
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
    const form = mockApi.postForm.mock.calls[0][1] as FormData;
    expect(form.get("name")).toBe("restored");
    expect(form.get("execution_backend")).toBe("host_process");
    expect(form.get("file")).toBeInstanceOf(File);
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

  it("surfaces an invalid export archive", async () => {
    mockApi.postForm.mockRejectedValue(
      new ApiError(422, { reason: "invalid_export_metadata" }),
    );
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

    expect(
      await screen.findByText(
        t("serverCreate.import.error.invalid_export_metadata"),
      ),
    ).toBeInTheDocument();
    expect(lastPath).toBe(`/communities/${CID}/servers/new`);
  });
});

describe("create-vs-import tab in the URL (#540)", () => {
  it("defaults to the new-server tab with a clean (hash-less) URL", async () => {
    renderPage();
    await screen.findByText(t("serverCreate.typeHeading"));
    expect(activeTab()).toBe(t("serverCreate.tab.new"));
    expect(lastHash).toBe("");
  });

  it("deep-links to the import tab via the #import hash", async () => {
    renderPage(`/communities/${CID}/servers/new#import`);
    expect(
      await screen.findByText(t("serverCreate.import.heading")),
    ).toBeInTheDocument();
    expect(activeTab()).toBe(t("serverCreate.tab.import"));
  });

  it("switching to the import tab writes the #import hash", async () => {
    renderPage();
    fireEvent.click(await screen.findByText(t("serverCreate.tab.import")));
    expect(activeTab()).toBe(t("serverCreate.tab.import"));
    expect(lastHash).toBe("#import");
  });

  it("switching back to the new-server tab clears the hash", async () => {
    renderPage(`/communities/${CID}/servers/new#import`);
    fireEvent.click(await screen.findByText(t("serverCreate.tab.new")));
    expect(activeTab()).toBe(t("serverCreate.tab.new"));
    expect(lastHash).toBe("");
  });
});
