import { describe, expect, it } from "vitest";
import openapi from "../../openapi.json";
import {
  MAX_CONSOLE_LINE_BYTES,
  MAX_EDIT_BYTES,
  MAX_WARNING_MESSAGE_BYTES,
  utf8ByteLength,
} from "./limits.ts";

type Schemas = Record<
  string,
  { properties: Record<string, { maxLength?: number; anyOf?: unknown[] }> }
>;
const schemas = openapi.components.schemas as unknown as Schemas;

describe("limits", () => {
  it("holds the console line limit the API declares", () => {
    expect(schemas.ServerCommandRequest.properties.line.maxLength).toBe(
      MAX_CONSOLE_LINE_BYTES,
    );
  });

  it("holds the warning message limit the API declares", () => {
    expect(schemas.WarningStepBody.properties.message.maxLength).toBe(
      MAX_WARNING_MESSAGE_BYTES,
    );
  });

  it("holds the edit cap the API's base64 bound is derived from", () => {
    // 4 characters of base64 per 3 bytes, the last group padded.
    expect(schemas.WriteFileRequest.properties.content_base64.maxLength).toBe(
      4 * Math.ceil(MAX_EDIT_BYTES / 3),
    );
  });

  it("measures text in UTF-8 bytes, not characters", () => {
    expect(utf8ByteLength("abc")).toBe(3);
    expect(utf8ByteLength("あ")).toBe(3);
    expect(utf8ByteLength("😀")).toBe(4);
  });
});
