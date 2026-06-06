import { describe, expect, it } from "vitest";
import { isSpaNavigation } from "./dev-proxy";

describe("isSpaNavigation", () => {
  it("treats an HTML-accepting request as a navigation", () => {
    expect(
      isSpaNavigation({
        accept:
          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      }),
    ).toBe(true);
  });

  it("treats a JSON fetch as an API request", () => {
    expect(isSpaNavigation({ accept: "application/json" })).toBe(false);
  });

  it("treats a wildcard Accept (the browser fetch default) as an API request", () => {
    expect(isSpaNavigation({ accept: "*/*" })).toBe(false);
  });

  it("treats a WebSocket upgrade as an API request even if it accepts HTML", () => {
    expect(isSpaNavigation({ accept: "text/html", upgrade: "websocket" })).toBe(
      false,
    );
  });

  it("does not fall through when no Accept header is present", () => {
    expect(isSpaNavigation({})).toBe(false);
  });

  it("joins a multi-value Accept header before matching", () => {
    expect(isSpaNavigation({ accept: ["application/json", "text/html"] })).toBe(
      true,
    );
  });
});
