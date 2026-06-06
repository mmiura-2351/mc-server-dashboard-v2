import { describe, expect, it } from "vitest";
import { resolvePermission } from "./resolve.ts";

const empty = { permissions: [], grants: [] };

describe("resolvePermission", () => {
  it("grants a community-wide code regardless of resource id", () => {
    const perms = { permissions: ["server:start"], grants: [] };
    expect(resolvePermission(perms, "server:start")).toBe(true);
    expect(resolvePermission(perms, "server:start", { serverId: "s1" })).toBe(
      true,
    );
  });

  it("denies a code absent from both community codes and grants", () => {
    expect(resolvePermission(empty, "server:start")).toBe(false);
    const perms = { permissions: ["server:read"], grants: [] };
    expect(resolvePermission(perms, "server:start")).toBe(false);
  });

  it("grants a per-resource code only for the matching resource id", () => {
    const perms = {
      permissions: [],
      grants: [
        {
          resource_type: "server",
          resource_id: "s1",
          permissions: ["server:start"],
        },
      ],
    };
    expect(resolvePermission(perms, "server:start", { serverId: "s1" })).toBe(
      true,
    );
    expect(resolvePermission(perms, "server:start", { serverId: "s2" })).toBe(
      false,
    );
  });

  it("does not let a resource grant satisfy a community-wide (no-resource) check", () => {
    const perms = {
      permissions: [],
      grants: [
        {
          resource_type: "server",
          resource_id: "s1",
          permissions: ["server:start"],
        },
      ],
    };
    // Without a resource id the question is "community-wide", which a
    // server-scoped grant must not answer.
    expect(resolvePermission(perms, "server:start")).toBe(false);
  });

  it("does not let one resource's grant leak to a different code", () => {
    const perms = {
      permissions: [],
      grants: [
        {
          resource_type: "server",
          resource_id: "s1",
          permissions: ["server:read"],
        },
      ],
    };
    expect(resolvePermission(perms, "server:start", { serverId: "s1" })).toBe(
      false,
    );
  });

  it("takes the union of community codes and matching grants", () => {
    const perms = {
      permissions: ["server:read"],
      grants: [
        {
          resource_type: "server",
          resource_id: "s1",
          permissions: ["server:start"],
        },
      ],
    };
    expect(resolvePermission(perms, "server:read", { serverId: "s1" })).toBe(
      true,
    );
    expect(resolvePermission(perms, "server:start", { serverId: "s1" })).toBe(
      true,
    );
  });
});
