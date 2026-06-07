import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { initLanguage, t } from "../i18n/index.ts";
import { ja } from "../i18n/ja.ts";
import { renderApp } from "../test/render.tsx";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function tokenResponse(): Response {
  return jsonResponse({
    access_token: "fresh",
    refresh_token: "ignored",
    token_type: "bearer",
  });
}

const fetchMock = vi.fn();

// Sign in via the bootstrap refresh and serve the given community list to the
// shared `GET /communities` query the switcher reads. Permission fetches per
// community are stubbed empty so capability loading does not error.
function signedInWith(communities: Array<{ id: string; name: string }>) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url === "/api/communities") {
      return Promise.resolve(jsonResponse(communities));
    }
    if (url.endsWith("/me/permissions")) {
      return Promise.resolve(jsonResponse({}));
    }
    if (url.endsWith("/servers")) {
      return Promise.resolve(jsonResponse([]));
    }
    return Promise.resolve(tokenResponse());
  });
}

function renderAt(path: string) {
  renderApp({ path });
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const ALPHA = { id: "alpha", name: "Alpha" };
const BETA = { id: "beta", name: "Beta" };

function dashboardLink(): HTMLAnchorElement {
  return screen.getByRole("link", {
    name: t("nav.dashboard"),
  }) as HTMLAnchorElement;
}

describe("AppShell community switcher", () => {
  it("defaults to the first community and targets its dashboard", async () => {
    signedInWith([ALPHA, BETA]);

    renderAt("/");

    const switcher = await screen.findByRole("combobox", {
      name: t("shell.switchCommunity"),
    });
    expect((switcher as HTMLSelectElement).value).toBe("alpha");
    expect(dashboardLink()).toHaveAttribute("href", "/communities/alpha");
  });

  it("switching updates the active community and re-targets the nav", async () => {
    signedInWith([ALPHA, BETA]);

    renderAt("/");

    const switcher = (await screen.findByRole("combobox", {
      name: t("shell.switchCommunity"),
    })) as HTMLSelectElement;

    fireEvent.change(switcher, { target: { value: "beta" } });

    await waitFor(() =>
      expect(dashboardLink()).toHaveAttribute("href", "/communities/beta"),
    );
    expect(switcher.value).toBe("beta");
  });

  it("a deep link selects the community it points at", async () => {
    signedInWith([ALPHA, BETA]);

    renderAt("/communities/beta");

    const switcher = await screen.findByRole("combobox", {
      name: t("shell.switchCommunity"),
    });
    await waitFor(() =>
      expect((switcher as HTMLSelectElement).value).toBe("beta"),
    );
    expect(dashboardLink()).toHaveAttribute("href", "/communities/beta");
  });

  it("renders chrome in Japanese when the language override is ja", async () => {
    localStorage.setItem("mcsd.lang", "ja");
    initLanguage();
    signedInWith([ALPHA, BETA]);

    renderAt("/");

    // The language selector and the account link render the ja dictionary.
    expect(
      await screen.findByRole("combobox", { name: ja["shell.language"] }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: ja["shell.account"] }),
    ).toBeInTheDocument();

    localStorage.clear();
    initLanguage();
  });

  it("switching the language persists the choice without reloading", async () => {
    signedInWith([ALPHA, BETA]);

    renderAt("/");

    const langSwitcher = (await screen.findByRole("combobox", {
      name: t("shell.language"),
    })) as HTMLSelectElement;

    const reload = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({
      ...window.location,
      reload,
    } as Location);

    fireEvent.change(langSwitcher, { target: { value: "ja" } });

    // The choice is persisted and applied in place — no full reload, which
    // would tear down an in-flight refresh rotation and could sign the user
    // out (issues #515, #512). The live re-render is covered by the i18n unit
    // test and the auth E2E.
    expect(localStorage.getItem("mcsd.lang")).toBe("ja");
    expect(reload).not.toHaveBeenCalled();

    localStorage.clear();
    initLanguage();
  });

  it("shows the no-communities state when the caller has none", async () => {
    signedInWith([]);

    renderAt("/");

    // Once the (empty) list resolves the switcher shows the empty label; the
    // sidebar then offers the no-communities hint instead of nav links.
    expect(await screen.findByText(t("shell.noCommunity"))).toBeInTheDocument();
    expect(screen.getByText(t("shell.noCommunities"))).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: t("nav.dashboard") }),
    ).not.toBeInTheDocument();
  });
});
