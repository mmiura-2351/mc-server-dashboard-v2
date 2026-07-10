/**
 * Shared `GET /users/me` query (#474). One cache entry keyed `["users","me"]`
 * so the account page, the admin gate, and any later admin page read the same
 * current user without duplicating the fetch. `is_platform_admin` drives the
 * admin-area gating (WEBUI_SPEC.md Section 3).
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client.ts";

export function useCurrentUser() {
  return useQuery({
    queryKey: ["users", "me"],
    queryFn: ({ signal }) => api.get("/api/users/me", { signal }),
  });
}
