import { describe, expect, it } from "vitest";
import { t } from "../i18n/index.ts";
import {
  applyAuditParams,
  operationLabel,
  targetTypeLabel,
} from "./auditShared.tsx";

describe("operationLabel", () => {
  it("maps a known operation code to its human-readable label", () => {
    // The mapped label is dictionary copy, not the raw code.
    expect(operationLabel("server:start")).toBe(
      t("communitySettings.audit.op.server:start"),
    );
    expect(operationLabel("server:start")).not.toBe("server:start");
  });

  it("maps the auth/file/server codes called out in the issue", () => {
    expect(operationLabel("file:search")).toBe(
      t("communitySettings.audit.op.file:search"),
    );
    expect(operationLabel("file:download")).toBe(
      t("communitySettings.audit.op.file:download"),
    );
    expect(operationLabel("server:command")).toBe(
      t("communitySettings.audit.op.server:command"),
    );
    expect(operationLabel("auth:session_restore")).toBe(
      t("communitySettings.audit.op.auth:session_restore"),
    );
  });

  it("falls back to the raw code for an unmapped/unknown operation", () => {
    expect(operationLabel("community.permission_grant_revoke")).toBe(
      "community.permission_grant_revoke",
    );
    expect(operationLabel("some:future_code")).toBe("some:future_code");
    expect(operationLabel("")).toBe("");
  });
});

describe("targetTypeLabel", () => {
  it("maps the type prefixes called out in the issue (file/user/server)", () => {
    expect(targetTypeLabel("file")).toBe(
      t("communitySettings.audit.targetType.file"),
    );
    expect(targetTypeLabel("user")).toBe(
      t("communitySettings.audit.targetType.user"),
    );
    expect(targetTypeLabel("server")).toBe(
      t("communitySettings.audit.targetType.server"),
    );
  });

  it("falls back to the raw type for an unmapped value", () => {
    expect(targetTypeLabel("future_type")).toBe("future_type");
  });
});

describe("applyAuditParams", () => {
  it("converts a valid datetime-local since/until to UTC ISO strings", () => {
    const params = new URLSearchParams();
    applyAuditParams(params, {
      operation: "",
      actor: "",
      since: "2024-01-15T10:00",
      until: "2024-01-16T12:00",
    });
    expect(params.has("since")).toBe(true);
    expect(params.has("until")).toBe(true);
    // Must be valid ISO 8601 (parseable without throwing).
    expect(() =>
      new Date(params.get("since") ?? "").toISOString(),
    ).not.toThrow();
    expect(() =>
      new Date(params.get("until") ?? "").toISOString(),
    ).not.toThrow();
  });

  it("ignores an invalid since value instead of throwing (#791)", () => {
    const params = new URLSearchParams();
    // A crafted/garbage URL param that Date() cannot parse.
    expect(() =>
      applyAuditParams(params, {
        operation: "",
        actor: "",
        since: "not-a-date",
        until: "",
      }),
    ).not.toThrow();
    expect(params.has("since")).toBe(false);
  });

  it("ignores an invalid until value instead of throwing (#791)", () => {
    const params = new URLSearchParams();
    expect(() =>
      applyAuditParams(params, {
        operation: "",
        actor: "",
        since: "",
        until: "garbage",
      }),
    ).not.toThrow();
    expect(params.has("until")).toBe(false);
  });

  it("omits since/until when the filter strings are empty", () => {
    const params = new URLSearchParams();
    applyAuditParams(params, {
      operation: "",
      actor: "",
      since: "",
      until: "",
    });
    expect(params.has("since")).toBe(false);
    expect(params.has("until")).toBe(false);
  });
});
