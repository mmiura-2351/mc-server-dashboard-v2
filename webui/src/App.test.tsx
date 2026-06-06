import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "./App.tsx";
import { t } from "./i18n/index.ts";

describe("App", () => {
  it("renders the dashboard heading from the dictionary", () => {
    render(<App />);
    expect(
      screen.getByRole("heading", { name: t("app.title") }),
    ).toBeInTheDocument();
  });
});
