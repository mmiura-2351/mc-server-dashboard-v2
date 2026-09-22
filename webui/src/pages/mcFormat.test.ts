// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import { stripMinecraftCodes } from "./mcFormat.ts";

describe("stripMinecraftCodes", () => {
  it("strips standard color, formatting, and reset codes", () => {
    expect(
      stripMinecraftCodes("§aGreen §lBold §oItalic §nUnderline §rplain"),
    ).toBe("Green Bold Italic Underline plain");
  });

  it("strips extended hex color codes before standard codes", () => {
    const raw =
      "§x§3§4§9§f§d§aℹ §fServer Plugins (3):\n" +
      "§x§E§D§8§1§0§6Bukkit Plugins:\n" +
      " §8- §afloodgate§r, §aGeyser-Spigot§r, §aGSit";
    const expected =
      "ℹ Server Plugins (3):\n" +
      "Bukkit Plugins:\n" +
      " - floodgate, Geyser-Spigot, GSit";
    expect(stripMinecraftCodes(raw)).toBe(expected);
  });
});
