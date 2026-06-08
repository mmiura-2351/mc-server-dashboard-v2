import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { LANDING_PATH } from "../routes.ts";
import { renderApp } from "../test/render.tsx";

// Not-found (404) route (#639). Driven through the real router via renderApp so
// the catch-all `*` route resolves; a 401 bootstrap keeps the session signed
// out, which is the worst case (no shell chrome) and is exactly when a "back
// home" link must still rescue the user.

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(new Response("", { status: 401 }));
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("not-found route", () => {
  it("shows real not-found copy, not the developer placeholder", async () => {
    renderApp({ path: "/this/route/does/not/exist" });

    expect(
      await screen.findByRole("heading", { name: t("page.notFound") }),
    ).toBeInTheDocument();
    expect(screen.getByText(t("notFound.body"))).toBeInTheDocument();
    // The developer placeholder copy must never reach a 404.
    expect(screen.queryByText(t("page.placeholder"))).not.toBeInTheDocument();
  });

  it("offers a link back home so the user is not stranded", async () => {
    renderApp({ path: "/this/route/does/not/exist" });

    const home = await screen.findByRole("link", { name: t("notFound.home") });
    expect(home).toHaveAttribute("href", LANDING_PATH);
  });
});
