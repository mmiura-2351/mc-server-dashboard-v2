// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import {
  ADMIN_COMMUNITIES_KEY,
  adminCommunitiesListKey,
  adminCommunitiesPickerKey,
} from "./adminQueryKeys.ts";

// Regression: the Communities page (list, limit=50) and the Audit community
// picker (limit=100) once shared the bare key ["admin","communities",0] at the
// first page, so React Query deduped them and served one the other's
// wrong-limit data. The two builders must never collide, yet both must stay
// under the shared prefix so a single invalidate refreshes both.
describe("admin community query keys", () => {
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

  it("both keys start with the shared prefix used for invalidation", () => {
    expect(adminCommunitiesListKey(50, 0).slice(0, 2)).toEqual([
      ...ADMIN_COMMUNITIES_KEY,
    ]);
    expect(adminCommunitiesPickerKey(100).slice(0, 2)).toEqual([
      ...ADMIN_COMMUNITIES_KEY,
    ]);
  });
});
