import { api } from "./client.ts";
import type { components } from "./schema";

// Exact type equality: `never`, `unknown` and `any` each match only themselves,
// so a response type that silently degrades to one of them fails here.
type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2
    ? true
    : false;
function expectTrue<_ extends true>(): void {}

type Schemas = components["schemas"];

const server = api.get("/api/communities/{community_id}/servers/{server_id}");
expectTrue<Equal<typeof server, Promise<Schemas["ServerResponse"]>>>();

// A 204 declares no JSON content.
const deleted = api.delete(
  "/api/communities/{community_id}/servers/{server_id}",
);
expectTrue<Equal<typeof deleted, Promise<undefined>>>();

// @ts-expect-error the path declares get only
api.put("/api/communities/{community_id}/me/permissions");

expectTrue<
  Equal<
    Schemas["ScheduleAction"],
    "command" | "start" | "stop" | "restart" | "backup"
  >
>();

expectTrue<Equal<Schemas["ServerResponse"]["crash_reason"], string | null>>();
