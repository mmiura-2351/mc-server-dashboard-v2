// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import { stripMinecraftCodes } from "./mcFormat.ts";

describe("stripMinecraftCodes", () => {
  it("strips standard color codes (§0–§9, §a–§f)", () => {
    expect(stripMinecraftCodes("§aGreen §4Red §fWhite")).toBe(
      "Green Red White",
    );
  });

  it("strips formatting codes (§k, §l, §m, §n, §o)", () => {
    expect(stripMinecraftCodes("§lBold §oItalic §nUnderline")).toBe(
      "Bold Italic Underline",
    );
  });

  it("strips the reset code (§r)", () => {
    expect(stripMinecraftCodes("§aGreen§r plain")).toBe("Green plain");
  });

  it("strips extended hex color codes (§x§R§R§G§G§B§B)", () => {
    expect(stripMinecraftCodes("§x§3§4§9§f§d§aColored text")).toBe(
      "Colored text",
    );
  });

  it("strips mixed standard and extended codes from real plugin output", () => {
    const raw =
      "§x§3§4§9§f§d§aℹ §fServer Plugins (3):\n" +
      "§x§e§d§8§1§0§6Bukkit Plugins:\n" +
      " §8- §afloodgate§r, §aGeyser-Spigot§r, §aGSit";
    const expected =
      "ℹ Server Plugins (3):\n" +
      "Bukkit Plugins:\n" +
      " - floodgate, Geyser-Spigot, GSit";
    expect(stripMinecraftCodes(raw)).toBe(expected);
  });

  it("returns text unchanged when no formatting codes are present", () => {
    const plain = "[12:00:00 INFO]: Player joined the game";
    expect(stripMinecraftCodes(plain)).toBe(plain);
  });

  it("handles uppercase hex digits in extended codes", () => {
    expect(stripMinecraftCodes("§x§A§B§C§D§E§FUpper")).toBe("Upper");
  });
});
