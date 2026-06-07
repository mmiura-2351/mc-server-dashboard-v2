import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { CommunitySettingsPage } from "./CommunitySettingsPage.tsx";

const CID = "c1";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
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

let mockCan: Can = () => true;
const setCommunityId = vi.fn();
vi.mock("../permissions/ActiveCommunityProvider.tsx", () => ({
  useActiveCommunity: () => ({
    communityId: CID,
    setCommunityId,
    communities: [{ id: CID, name: "Sakura" }],
  }),
}));
vi.mock("../permissions/useCan.ts", () => ({ useCan: () => mockCan }));

function community() {
  return { id: CID, name: "Sakura" };
}

function record(over: Record<string, unknown> = {}) {
  return {
    id: "a1",
    operation: "server:start",
    outcome: "success",
    created_at: "2026-06-06T12:00:00Z",
    actor_id: "u1",
    community_id: CID,
    target_type: "server",
    target_id: "s1",
    ...over,
  };
}

// Route `api.get` by path: the page reads the bare community, the Audit tab
// reads the community audit endpoint (path + query string).
function routeGet(opts: { records?: unknown[] }) {
  mockApi.get.mockImplementation((path: string) => {
    if (path.startsWith(`/api/communities/${CID}/audit`)) {
      return Promise.resolve({ records: opts.records ?? [] });
    }
    return Promise.resolve(community());
  });
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={[`/communities/${CID}/settings`]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <Routes>
            <Route
              path="/communities/:cid/settings"
              element={<CommunitySettingsPage />}
            />
          </Routes>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

// Open the Audit tab (the default landing tab is Members).
async function openAuditTab() {
  await screen.findAllByText("Sakura");
  fireEvent.click(
    screen.getByRole("tab", { name: t("communitySettings.tab.audit") }),
  );
}

// Pull the audit-endpoint calls (the path carries the query string).
function auditCalls(): string[] {
  return mockApi.get.mock.calls
    .map((c) => c[0] as string)
    .filter((p) => p.startsWith(`/api/communities/${CID}/audit`));
}

describe("CommunityAuditTab", () => {
  beforeEach(() => {
    setAccessToken("tok-1");
    mockApi.get.mockReset();
    mockApi.post.mockReset();
    mockApi.patch.mockReset();
    mockApi.put.mockReset();
    mockApi.delete.mockReset();
    mockCan = () => true;
    setCommunityId.mockReset();
  });
  afterEach(() => vi.clearAllMocks());

  it("renders an entry's timestamp, actor, operation and target readably", async () => {
    routeGet({
      records: [
        record({
          operation: "server:start",
          actor_id: "u1",
          target_type: "server",
          target_id: "s1",
        }),
      ],
    });
    renderPage();
    await openAuditTab();

    expect(await screen.findByText("server:start")).toBeInTheDocument();
    expect(screen.getByText("u1")).toBeInTheDocument();
    expect(screen.getByText("success")).toBeInTheDocument();
    // Target rendered as "type:id".
    expect(screen.getByText("server:s1")).toBeInTheDocument();
  });

  it("carries the full value as a hover title on the long-value cells", async () => {
    const actor = "22222222-2222-2222-2222-222222222222";
    routeGet({
      records: [
        record({
          operation: "community.permission_grant_revoke",
          actor_id: actor,
          target_type: "server",
          target_id: "s1",
        }),
      ],
    });
    renderPage();
    await openAuditTab();

    const op = await screen.findByText("community.permission_grant_revoke");
    expect(op.closest("td")).toHaveAttribute(
      "title",
      "community.permission_grant_revoke",
    );
    expect(screen.getByText(actor).closest("td")).toHaveAttribute(
      "title",
      actor,
    );
    expect(screen.getByText("server:s1").closest("td")).toHaveAttribute(
      "title",
      "server:s1",
    );
  });

  it("shows the empty state when there are no records", async () => {
    routeGet({ records: [] });
    renderPage();
    await openAuditTab();

    expect(
      await screen.findByText(t("communitySettings.audit.empty")),
    ).toBeInTheDocument();
  });

  it("requests the first page with limit and offset only", async () => {
    routeGet({ records: [record()] });
    renderPage();
    await openAuditTab();

    await waitFor(() => expect(auditCalls().length).toBeGreaterThan(0));
    const url = new URL(auditCalls()[0], "http://x");
    expect(url.pathname).toBe(`/api/communities/${CID}/audit`);
    expect(url.searchParams.get("limit")).toBe("50");
    expect(url.searchParams.get("offset")).toBe("0");
    expect(url.searchParams.get("operation")).toBeNull();
    expect(url.searchParams.get("actor")).toBeNull();
    expect(url.searchParams.get("since")).toBeNull();
    expect(url.searchParams.get("until")).toBeNull();
  });

  it("maps the filter inputs to operation/actor/since/until query params", async () => {
    routeGet({ records: [record()] });
    renderPage();
    await openAuditTab();
    await screen.findByText("server:start");

    fireEvent.change(
      screen.getByLabelText(t("communitySettings.audit.filterOperation")),
      { target: { value: "member:add" } },
    );
    const ACTOR = "11111111-1111-1111-1111-111111111111";
    const SINCE_LOCAL = "2026-01-01T00:00";
    const UNTIL_LOCAL = "2026-02-01T00:00";
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.audit.filterActor")),
      { target: { value: ACTOR } },
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.audit.filterSince")),
      { target: { value: SINCE_LOCAL } },
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.audit.filterUntil")),
      { target: { value: UNTIL_LOCAL } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.audit.apply"),
      }),
    );

    await waitFor(() => {
      const url = new URL(auditCalls().at(-1) as string, "http://x");
      expect(url.searchParams.get("operation")).toBe("member:add");
      expect(url.searchParams.get("actor")).toBe(ACTOR);
      // since/until are sent as UTC instants; derive the expectation via the
      // same local→UTC conversion so the test is timezone-independent.
      expect(url.searchParams.get("since")).toBe(
        new Date(SINCE_LOCAL).toISOString(),
      );
      expect(url.searchParams.get("until")).toBe(
        new Date(UNTIL_LOCAL).toISOString(),
      );
      expect(url.searchParams.get("offset")).toBe("0");
    });
  });

  it("rejects a non-UUID actor inline without issuing a request", async () => {
    routeGet({ records: [record()] });
    renderPage();
    await openAuditTab();
    await screen.findByText("server:start");
    const before = auditCalls().length;

    fireEvent.change(
      screen.getByLabelText(t("communitySettings.audit.filterActor")),
      { target: { value: "not-a-uuid" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.audit.apply"),
      }),
    );

    expect(
      await screen.findByText(t("communitySettings.audit.filterActorInvalid")),
    ).toBeInTheDocument();
    // No new audit request carried the bad value.
    expect(auditCalls().length).toBe(before);
  });

  it("pages forward with an increased offset", async () => {
    // A full page (50) signals there may be a next page.
    routeGet({
      records: Array.from({ length: 50 }, (_, i) => record({ id: `a${i}` })),
    });
    renderPage();
    await openAuditTab();
    await screen.findAllByText("server:start");

    // First page requests offset 0.
    await waitFor(() => {
      const first = new URL(auditCalls()[0], "http://x");
      expect(first.searchParams.get("offset")).toBe("0");
    });

    fireEvent.click(
      screen.getByRole("button", { name: t("communitySettings.audit.next") }),
    );
    await waitFor(() => {
      const url = new URL(auditCalls().at(-1) as string, "http://x");
      expect(url.searchParams.get("offset")).toBe("50");
    });

    // Prev becomes enabled once past the first page, to return toward offset 0.
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: t("communitySettings.audit.prev"),
        }),
      ).not.toBeDisabled(),
    );
  });

  it("shows the error state when the list fails", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.startsWith(`/api/communities/${CID}/audit`)) {
        return Promise.reject(new ApiError(500, undefined));
      }
      return Promise.resolve(community());
    });
    renderPage();
    await openAuditTab();

    expect(
      await screen.findByText(t("communitySettings.audit.loadError")),
    ).toBeInTheDocument();
  });

  it("shows the denied notice when audit:read is absent", async () => {
    mockCan = (code: string) => code !== "audit:read";
    routeGet({ records: [] });
    renderPage();
    await openAuditTab();

    expect(
      await screen.findByText(t("permissions.denied")),
    ).toBeInTheDocument();
    expect(auditCalls().length).toBe(0);
  });

  it("routes a 403 from the list through onForbidden", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.startsWith(`/api/communities/${CID}/audit`)) {
        return Promise.reject(new ApiError(403, { reason: "audit:read" }));
      }
      return Promise.resolve(community());
    });
    renderPage();
    await openAuditTab();

    expect(
      await screen.findByText(`${t("permissions.deniedNamed")}audit:read`),
    ).toBeInTheDocument();
  });
});
