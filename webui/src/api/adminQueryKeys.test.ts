// @vitest-environment node
import { describe, expect, it } from "vitest";
import {
  ADMIN_COMMUNITIES_KEY,
  adminCommunitiesListKey,
  adminCommunitiesPickerKey,
} from "./adminQueryKeys.ts";

// Different page sizes must not collide, but both keys must share an invalidation prefix.
describe("admin community query keys", () => {
  it.each([
    [
      "list",
      adminCommunitiesListKey(50, 0),
      [...ADMIN_COMMUNITIES_KEY, "list", { limit: 50, offset: 0 }],
    ],
    [
      "picker",
      adminCommunitiesPickerKey(100),
      [...ADMIN_COMMUNITIES_KEY, "picker", { limit: 100 }],
    ],
  ])("encodes the %s request shape", (_name, actual, expected) => {
    expect(actual).toEqual(expected);
  });

  it("the list and picker first-page keys are distinct", () => {
    expect(adminCommunitiesListKey(50, 0)).not.toEqual(
      adminCommunitiesPickerKey(100),
    );
  });

  it("distinguishes list pages by limit and offset", () => {
    expect(adminCommunitiesListKey(50, 0)).not.toEqual(
      adminCommunitiesListKey(50, 50),
    );
    expect(adminCommunitiesListKey(50, 0)).not.toEqual(
      adminCommunitiesListKey(100, 0),
    );
  });
});
