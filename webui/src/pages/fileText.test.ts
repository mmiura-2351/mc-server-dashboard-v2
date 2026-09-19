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

describe("fileText base64 ⇄ UTF-8", () => {
  it("round-trips ASCII text", () => {
    const text = "level-name=world\nmax-players=20\n";
    const decoded = decodeBase64Text(encodeUtf8Base64(text));
    expect(decoded).toEqual({ text, charset: "utf-8" });
    expect(encodeTextBase64(decoded.text, decoded.charset)).toBe(
      encodeUtf8Base64(text),
    );
  });

  it("round-trips multi-byte UTF-8 (emoji + CJK) without mangling", () => {
    const text = "MOTD: ようこそ 🐉 サーバーへ";
    const base64 = encodeUtf8Base64(text);
    // A bare btoa(text) would throw on these code points; the helper must not.
    const decoded = decodeBase64Text(base64);
    expect(decoded).toEqual({ text, charset: "utf-8" });
    expect(encodeTextBase64(decoded.text, decoded.charset)).toBe(base64);
  });

  it("decodes a known UTF-8 payload byte-faithfully", () => {
    // "é" is 0xC3 0xA9 in UTF-8 → base64 "w6k=".
    expect(decodeBase64Text("w6k=")).toEqual({ text: "é", charset: "utf-8" });
  });

  it("writes a UTF-8 file's text back as UTF-8, not one byte per character", () => {
    expect(encodeTextBase64("é", "utf-8")).toBe("w6k=");
  });
});

describe("fileText base64 ⇄ latin-1 fallback (#2851)", () => {
  it("reads bytes that are not valid UTF-8 as latin-1", () => {
    // A lone 0xE9 is "é" in latin-1 and a malformed sequence in UTF-8, which a
    // UTF-8-only decode turned into U+FFFD.
    expect(decodeBase64Text("6Q==")).toEqual({
      text: "é",
      charset: "latin-1",
    });
  });

  it("round-trips every byte of a non-UTF-8 file unchanged", () => {
    // All 256 byte values, 0x80-0x9F included: a WHATWG "latin1" TextDecoder is
    // really windows-1252 and would map those onto other code points.
    const base64 = btoa(
      String.fromCharCode(...Array.from({ length: 256 }, (_, i) => i)),
    );
    const decoded = decodeBase64Text(base64);
    expect(decoded.charset).toBe("latin-1");
    expect(encodeTextBase64(decoded.text, decoded.charset)).toBe(base64);
  });

  it("writes latin-1 text one byte per character", () => {
    expect(encodeTextBase64("é", "latin-1")).toBe("6Q==");
  });

  it("refuses a character latin-1 cannot hold", () => {
    expect(() => encodeTextBase64("motd=Café 🐉", "latin-1")).toThrow(
      UnencodableTextError,
    );
  });
});

describe("isProbablyText", () => {
  it("treats NUL-free content as text", () => {
    expect(isProbablyText(encodeUtf8Base64("plain text"))).toBe(true);
  });

  it("treats unicode text as text", () => {
    expect(isProbablyText(encodeUtf8Base64("絵文字 🎮"))).toBe(true);
  });

  it("treats content with a NUL byte as binary", () => {
    // bytes [0x66, 0x00, 0x66] = "f\0f" → base64.
    const base64 = btoa(String.fromCharCode(0x66, 0x00, 0x66));
    expect(isProbablyText(base64)).toBe(false);
  });
});
