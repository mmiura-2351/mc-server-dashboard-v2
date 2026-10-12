import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client.ts";

export function useCurrentUser() {
  return useQuery({
    queryKey: ["users", "me"],
    queryFn: ({ signal }) => api.get("/api/users/me", { signal }),
  });
}
