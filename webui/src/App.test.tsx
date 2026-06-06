import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { App } from "./App.tsx";
import { t } from "./i18n/index.ts";

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("App routing", () => {
  it("renders the dashboard inside the shell chrome", () => {
    renderAt("/communities/demo");

    // Shell chrome: sidebar nav + top bar user menu.
    expect(
      screen.getByRole("link", { name: t("nav.dashboard") }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: t("page.dashboard") }),
    ).toBeInTheDocument();
  });

  it("renders an admin placeholder inside the shell", () => {
    renderAt("/admin/workers");

    expect(
      screen.getByRole("heading", { name: t("page.adminWorkers") }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: t("nav.adminWorkers") }),
    ).toBeInTheDocument();
  });

  it("renders the login page without the shell chrome", () => {
    renderAt("/login");

    expect(
      screen.getByRole("heading", { name: t("page.login") }),
    ).toBeInTheDocument();
    // The sidebar nav is absent outside the shell.
    expect(
      screen.queryByRole("link", { name: t("nav.dashboard") }),
    ).not.toBeInTheDocument();
  });
});
