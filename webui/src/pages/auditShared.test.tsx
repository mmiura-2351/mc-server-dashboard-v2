import { describe, expect, it } from "vitest";
import { t } from "../i18n/index.ts";
import { operationLabel, targetTypeLabel } from "./auditShared.tsx";

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
