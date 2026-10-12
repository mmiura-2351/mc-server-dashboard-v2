/** Files travel as base64 bytes; decode UTF-8 when valid and otherwise Latin-1, then save in the same charset.
 * Pre-1.20 root server.properties is always Latin-1 because that is how Minecraft reads it. */

import { readsServerPropertiesAsUtf8 } from "../mcVersion.ts";

const SNIFF_BYTES = 8192;

function base64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

/** Retain the read charset for writing the edited file. */
export type TextCharset = "utf-8" | "latin-1";

export interface DecodedText {
  text: string;
  charset: TextCharset;
}

export class UnencodableTextError extends Error {
  constructor() {
    super("text holds a character outside latin-1");
    this.name = "UnencodableTextError";
  }
}

export function decodeBase64Text(
  base64: string,
  file: { path: string; mcVersion: string | null | undefined },
): DecodedText {
  // Normalize empty and dot components like the API before identifying the root server.properties file.
  const parts = file.path.split("/").filter((p) => p !== "" && p !== ".");
  const latin1Only =
    parts.length === 1 &&
    parts[0] === "server.properties" &&
    !readsServerPropertiesAsUtf8(file.mcVersion);
  if (!latin1Only) {
    try {
      const text = new TextDecoder("utf-8", { fatal: true }).decode(
        base64ToBytes(base64),
      );
      return { text, charset: "utf-8" };
    } catch {
      // Failed UTF-8 decoding falls back to Latin-1 below.
    }
  }
  // atob maps bytes directly to Latin-1; TextDecoder("latin1") instead uses Windows-1252.
  return { text: atob(base64), charset: "latin-1" };
}

export function encodeUtf8Base64(text: string): string {
  return bytesToBase64(new TextEncoder().encode(text));
}

/** Throw UnencodableTextError for characters above U+00FF when saving Latin-1. */
export function encodeTextBase64(text: string, charset: TextCharset): string {
  if (charset === "utf-8") {
    return encodeUtf8Base64(text);
  }
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) > 0xff) {
      throw new UnencodableTextError();
    }
  }
  return btoa(text);
}

/** Treat a NUL byte in the decoded prefix as a binary-file signal. */
export function isProbablyText(base64: string): boolean {
  const bytes = base64ToBytes(base64);
  const limit = Math.min(bytes.length, SNIFF_BYTES);
  for (let i = 0; i < limit; i++) {
    if (bytes[i] === 0) {
      return false;
    }
  }
  return true;
}
