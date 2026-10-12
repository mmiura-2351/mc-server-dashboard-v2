import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { t } from "../i18n/index.ts";
import { ErrorBoundary } from "./ErrorBoundary.tsx";

function Bomb(): never {
  throw new Error("boom");
}

describe("ErrorBoundary", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders children when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>ok</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("ok")).toBeInTheDocument();
  });

  it("shows a recovery UI when a child throws during render", () => {
    // Suppress React's expected render-error diagnostics in boundary tests.
    vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    );

    expect(screen.getByText(t("errorBoundary.title"))).toBeInTheDocument();
    expect(screen.getByText(t("errorBoundary.body"))).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: t("errorBoundary.reload") }),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});
