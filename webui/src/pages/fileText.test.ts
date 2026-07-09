// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import {
  decodeBase64Utf8,
  encodeUtf8Base64,
  isProbablyText,
} from "./fileText.ts";

describe("fileText base64 ⇄ UTF-8", () => {
  it("round-trips ASCII text", () => {
    const text = "level-name=world\nmax-players=20\n";
    expect(decodeBase64Utf8(encodeUtf8Base64(text))).toBe(text);
  });

  it("round-trips multi-byte UTF-8 (emoji + CJK) without mangling", () => {
    const text = "MOTD: ようこそ 🐉 サーバーへ";
    const base64 = encodeUtf8Base64(text);
    // A bare btoa(text) would throw on these code points; the helper must not.
    expect(decodeBase64Utf8(base64)).toBe(text);
  });

  it("decodes a known UTF-8 payload byte-faithfully", () => {
    // "é" is 0xC3 0xA9 in UTF-8 → base64 "w6k=".
    expect(decodeBase64Utf8("w6k=")).toBe("é");
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
