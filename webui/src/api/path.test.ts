import { describe, expect, it } from "vitest";
import { apiPath } from "./path.ts";

describe("apiPath", () => {
  it("interpolates a single named parameter", () => {
    expect(
      apiPath("/api/communities/{community_id}/me/permissions", {
        community_id: "c1",
      }),
    ).toBe("/api/communities/c1/me/permissions");
  });

  it("interpolates multiple named parameters", () => {
    expect(
      apiPath("/api/communities/{community_id}/grants/{grant_id}", {
        community_id: "c1",
        grant_id: "g2",
      }),
    ).toBe("/api/communities/c1/grants/g2");
  });

  it("URL-encodes interpolated values", () => {
    expect(
      apiPath("/api/communities/{community_id}/me/permissions", {
        community_id: "a/b c?d",
      }),
    ).toBe("/api/communities/a%2Fb%20c%3Fd/me/permissions");
  });

  it("returns a parameterless path unchanged", () => {
    expect(apiPath("/api/communities", {})).toBe("/api/communities");
  });

  it("rejects a misspelled param name at the type level", () => {
    apiPath("/api/communities/{community_id}/me/permissions", {
      // @ts-expect-error wrong param name: the template declares community_id
      communityId: "c1",
    });
  });

  it("rejects a missing param at the type level", () => {
    // @ts-expect-error grant_id is required by the template
    apiPath("/api/communities/{community_id}/grants/{grant_id}", {
      community_id: "c1",
    });
  });
});
