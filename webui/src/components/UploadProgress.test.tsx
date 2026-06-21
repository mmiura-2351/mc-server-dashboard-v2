import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { UploadProgress } from "./UploadProgress.tsx";

describe("UploadProgress", () => {
  it("renders an accessible progress bar with the current percentage", () => {
    render(
      <UploadProgress
        loaded={512}
        total={1024}
        percent={50}
        elapsedMs={2000}
      />,
    );

    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "50");
    expect(bar).toHaveAttribute("aria-valuemin", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "100");
  });

  it("shows the percentage, transferred bytes, and elapsed time", () => {
    render(
      <UploadProgress
        loaded={512}
        total={1024}
        percent={50}
        elapsedMs={3000}
      />,
    );

    expect(screen.getByText("50%")).toBeInTheDocument();
    expect(screen.getByText("512 B / 1.0 KiB")).toBeInTheDocument();
    expect(screen.getByText("3s elapsed")).toBeInTheDocument();
  });

  it("fills the bar to the given percentage", () => {
    render(
      <UploadProgress loaded={768} total={1024} percent={75} elapsedMs={0} />,
    );

    const fill = screen
      .getByRole("progressbar")
      .querySelector(".upload-bar-fill");
    expect(fill).toHaveStyle({ width: "75%" });
  });
});
