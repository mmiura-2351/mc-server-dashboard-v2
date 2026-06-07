import { fireEvent, screen, waitFor } from "@testing-library/react";
import { useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { initLanguage, t } from "../i18n/index.ts";
import { ja } from "../i18n/ja.ts";
import { LANDING_PATH } from "../routes.ts";
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

// Surfaces the live URL so brand navigation can be asserted without depending
// on a guarded page's content.
function LocationProbe() {
  const { pathname } = useLocation();
  return <span data-testid="url">{pathname}</span>;
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
    const noCommunityLabel = await screen.findByText(t("shell.noCommunity"));
    expect(noCommunityLabel).toBeInTheDocument();
    // The label must not carry the generic `empty` class: it collides with the
    // empty-state CTA rule (shell.css `.empty { padding: 48px 20px }`), which
    // overrode the switcher's padding and overflowed the top bar (#533).
    expect(noCommunityLabel).not.toHaveClass("empty");
    // The sidebar hint must carry the `nav-hint` class: the narrow-width
    // breakpoint rule hides `.nav-hint` so this long prose string does not
    // become the widest rail child and reintroduce horizontal overflow when a
    // no-community user is on a phone (#586).
    const hint = screen.getByText(t("shell.noCommunities"));
    expect(hint).toBeInTheDocument();
    expect(hint).toHaveClass("nav-hint");
    expect(
      screen.queryByRole("link", { name: t("nav.dashboard") }),
    ).not.toBeInTheDocument();
  });

  it("wraps the account link label so it can collapse at narrow widths", async () => {
    signedInWith([ALPHA, BETA]);

    renderAt("/");

    // The account link keeps its always-visible avatar but carries its text in a
    // dedicated `.label` span, which the narrow-width topbar rule hides so the
    // trailing items stop overflowing off-screen (#554). The accessible name
    // still resolves from the avatar's text, so the link stays labelled.
    const account = await screen.findByRole("link", {
      name: t("shell.account"),
    });
    const label = account.querySelector(".label");
    expect(label).not.toBeNull();
    expect(label).toHaveTextContent(t("shell.account"));
  });

  it("wraps each nav label so the sidebar can collapse to an icon rail", async () => {
    signedInWith([ALPHA, BETA]);

    renderAt("/");

    // At narrow widths the sidebar collapses to an icon-only rail: the nav text
    // moves into a dedicated `.label` span the breakpoint rule hides, while the
    // icon stays. The link keeps an `aria-label`, so its accessible name still
    // resolves once the visible text is hidden (#586, mirroring the #554
    // account-link treatment).
    const dashboard = await screen.findByRole("link", {
      name: t("nav.dashboard"),
    });
    const label = dashboard.querySelector(".label");
    expect(label).not.toBeNull();
    expect(label).toHaveTextContent(t("nav.dashboard"));
    expect(dashboard).toHaveAttribute("aria-label", t("nav.dashboard"));
  });

  it("clicking the brand navigates to the landing page", async () => {
    signedInWith([ALPHA, BETA]);

    renderApp({ path: "/account", extras: <LocationProbe /> });

    const brand = await screen.findByRole("link", { name: t("shell.brand") });
    expect(brand).toHaveAttribute("href", LANDING_PATH);

    fireEvent.click(brand);

    // LANDING_PATH resolves the active community and redirects to its dashboard,
    // so a click from /account lands on the active community's dashboard.
    await waitFor(() =>
      expect(screen.getByTestId("url")).toHaveTextContent("/communities/alpha"),
    );
  });
});
