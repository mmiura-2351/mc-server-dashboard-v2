import { apiPath } from "./path.ts";

apiPath("/api/communities/{community_id}/me/permissions", {
  // @ts-expect-error wrong param name: the template declares community_id
  communityId: "c1",
});

// @ts-expect-error grant_id is required by the template
apiPath("/api/communities/{community_id}/grants/{grant_id}", {
  community_id: "c1",
});
