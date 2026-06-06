import { describe, expect, it } from "vitest";
import { t } from "./index.ts";

describe("t", () => {
  it("returns the English string for a known key", () => {
    expect(t("app.title")).toBe("mc-server-dashboard");
  });
});
