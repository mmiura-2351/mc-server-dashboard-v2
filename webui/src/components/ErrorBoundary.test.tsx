import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { t } from "../i18n/index.ts";
import { ErrorBoundary } from "./ErrorBoundary.tsx";

// Throws on render so the boundary catches it.
function Bomb(): never {
  throw new Error("boom");
}

describe("ErrorBoundary", () => {
  it("renders children when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>ok</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("ok")).toBeInTheDocument();
  });

  it("shows a recovery UI when a child throws during render", () => {
    // Suppress the React error-boundary console noise.
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
    // Marked as an alert so screen readers announce the error.
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});
