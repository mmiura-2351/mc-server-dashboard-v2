// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import {
  decodeBase64Text,
  encodeTextBase64,
  encodeUtf8Base64,
  isProbablyText,
  UnencodableTextError,
} from "./fileText.ts";

// A file the content-based charset rule governs: anything but the root
// server.properties of a pre-1.20 server.
const FILE = { path: "config.yml", mcVersion: "1.21.6" };

describe("fileText base64 ⇄ UTF-8", () => {
  it("round-trips multi-byte UTF-8 (emoji + CJK) without mangling", () => {
    const text = "MOTD: ようこそ 🐉 サーバーへ";
    const base64 = encodeUtf8Base64(text);
    // A bare btoa(text) would throw on these code points; the helper must not.
    const decoded = decodeBase64Text(base64, FILE);
    expect(decoded).toEqual({ text, charset: "utf-8" });
    expect(encodeTextBase64(decoded.text, decoded.charset)).toBe(base64);
  });
});

describe("fileText base64 ⇄ latin-1 fallback (#2851)", () => {
  it("round-trips every byte of a non-UTF-8 file unchanged", () => {
    // All 256 byte values, 0x80-0x9F included: a WHATWG "latin1" TextDecoder is
    // really windows-1252 and would map those onto other code points.
    const base64 = btoa(
      String.fromCharCode(...Array.from({ length: 256 }, (_, i) => i)),
    );
    const decoded = decodeBase64Text(base64, FILE);
    expect(decoded.charset).toBe("latin-1");
    expect(encodeTextBase64(decoded.text, decoded.charset)).toBe(base64);
  });

  it("refuses a character latin-1 cannot hold", () => {
    expect(() => encodeTextBase64("motd=Café 🐉", "latin-1")).toThrow(
      UnencodableTextError,
    );
  });
});

describe("fileText root server.properties before Minecraft 1.20 (#2851)", () => {
  // A pre-1.20 server reads it with Properties.load(InputStream): latin-1
  // only, whatever its bytes are.
  it("reads it as latin-1 even when its bytes are valid UTF-8", () => {
    // C3 A9 is "é" in UTF-8 but "Ã©" to a latin-1 reader.
    expect(
      decodeBase64Text("w6k=", {
        path: "server.properties",
        mcVersion: "1.19.4",
      }),
    ).toEqual({ text: "Ã©", charset: "latin-1" });
  });

  it("reads it by content on a 1.20+ server", () => {
    expect(
      decodeBase64Text("w6k=", {
        path: "server.properties",
        mcVersion: "1.20",
      }),
    ).toEqual({ text: "é", charset: "utf-8" });
  });

  it("reads a root alias the API resolves to server.properties as latin-1", () => {
    // The API's RelPath drops "." components and empty ones (redundant or
    // trailing separators), so each of these opens the root file.
    for (const path of [
      "./server.properties",
      "server.properties/",
      ".//server.properties",
    ]) {
      expect(decodeBase64Text("w6k=", { path, mcVersion: "1.19.4" })).toEqual({
        text: "Ã©",
        charset: "latin-1",
      });
    }
  });

  it("reads a nested server.properties by content", () => {
    expect(
      decodeBase64Text("w6k=", {
        path: "backup/server.properties",
        mcVersion: "1.19.4",
      }),
    ).toEqual({ text: "é", charset: "utf-8" });
  });
});

describe("isProbablyText", () => {
  it("treats NUL-free content as text", () => {
    expect(isProbablyText(encodeUtf8Base64("plain text"))).toBe(true);
  });

  it("treats content with a NUL byte as binary", () => {
    // bytes [0x66, 0x00, 0x66] = "f\0f" → base64.
    const base64 = btoa(String.fromCharCode(0x66, 0x00, 0x66));
    expect(isProbablyText(base64)).toBe(false);
  });
});
