import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
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

  it("renders a cancel button when onCancel is provided", () => {
    render(
      <UploadProgress
        loaded={512}
        total={1024}
        percent={50}
        elapsedMs={1000}
        onCancel={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("calls onCancel when the cancel button is clicked", () => {
    const onCancel = vi.fn();
    render(
      <UploadProgress
        loaded={512}
        total={1024}
        percent={50}
        elapsedMs={1000}
        onCancel={onCancel}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("does not render a cancel button when onCancel is omitted", () => {
    render(
      <UploadProgress
        loaded={512}
        total={1024}
        percent={50}
        elapsedMs={1000}
      />,
    );

    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });
});
