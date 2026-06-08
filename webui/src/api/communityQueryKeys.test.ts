import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import {
  attachmentsKeys,
  groupsKeys,
  membersKeys,
  rolesKeys,
} from "./communityQueryKeys.ts";

const CID = "c1";

// Seed a query into the cache as fresh (not stale), then return a probe that
// reports whether it is currently stale. invalidateQueries marks matching
// queries stale, so a seeded-fresh query going stale proves the invalidation's
// key prefix matched it.
function seedFresh(
  client: QueryClient,
  key: readonly unknown[],
): () => boolean {
  client.setQueryData([...key], "seed");
  const state = client.getQueryCache().find({ queryKey: [...key] });
  return () => state?.isStale() ?? true;
}

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

// These assert the cross-tab coherence the issue is about: a mutation's
// invalidation on a shared key marks a sibling tab's query stale.
describe("cross-tab cache coherence", () => {
  it("#472: a role invalidation marks the Members role chips (members) stale", () => {
    const client = new QueryClient();
    const membersStale = seedFresh(client, membersKeys.list(CID));

    // CommunityRolesTab's role mutation invalidates both lists.
    client.invalidateQueries({ queryKey: rolesKeys.list(CID) });
    client.invalidateQueries({ queryKey: membersKeys.list(CID) });

    expect(membersStale()).toBe(true);
  });

  it("#469: a Groups-tab attach invalidation marks the Players-tab list stale", () => {
    const client = new QueryClient();
    const playerListStale = seedFresh(
      client,
      attachmentsKeys.forServer(CID, "s1"),
    );

    // CommunityGroupsTab attach/detach invalidates the whole attachment prefix.
    client.invalidateQueries({ queryKey: attachmentsKeys.all(CID) });

    expect(playerListStale()).toBe(true);
  });

  it("#469: a Players-tab attach invalidation marks the Groups-tab list stale", () => {
    const client = new QueryClient();
    const groupServersStale = seedFresh(
      client,
      attachmentsKeys.forGroup(CID, "g1"),
    );

    // ServerPlayersTab attach/detach invalidates the whole attachment prefix.
    client.invalidateQueries({ queryKey: attachmentsKeys.all(CID) });

    expect(groupServersStale()).toBe(true);
  });

  it("does not over-invalidate an unrelated community's attachments", () => {
    const client = new QueryClient();
    const otherStale = seedFresh(
      client,
      attachmentsKeys.forServer("other", "s1"),
    );

    client.invalidateQueries({ queryKey: attachmentsKeys.all(CID) });

    expect(otherStale()).toBe(false);
  });
});
