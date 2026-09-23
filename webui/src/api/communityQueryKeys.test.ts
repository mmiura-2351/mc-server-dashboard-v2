// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import {
  attachmentsKeys,
  groupsKeys,
  rolesKeys,
} from "./communityQueryKeys.ts";

const CID = "c1";

describe("community query key factories", () => {
  it("scope keys by community id", () => {
    expect(rolesKeys.list(CID)).not.toEqual(rolesKeys.list("other"));
    expect(groupsKeys.list(CID)).not.toEqual(groupsKeys.list("other"));
    expect(attachmentsKeys.all(CID)).not.toEqual(attachmentsKeys.all("other"));
  });

  it("keeps both attachment projections under the shared all() prefix", () => {
    const prefix = attachmentsKeys.all(CID);
    expect(attachmentsKeys.forGroup(CID, "g1").slice(0, prefix.length)).toEqual(
      [...prefix],
    );
    expect(
      attachmentsKeys.forServer(CID, "s1").slice(0, prefix.length),
    ).toEqual([...prefix]);
  });

  it("distinguishes the group and server attachment projections", () => {
    expect(attachmentsKeys.forGroup(CID, "x")).not.toEqual(
      attachmentsKeys.forServer(CID, "x"),
    );
  });
});
