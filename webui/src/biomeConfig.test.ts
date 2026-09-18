// @vitest-environment node
// Pins biome.json's $schema to the schema shipped by the installed Biome
// (issue #3043). A versioned biomejs.dev URL drifts on every Biome bump and the
// resulting mismatch is only an info diagnostic, so `biome check` stays green.
import { expect, it } from "vitest";
import biomeJson from "../biome.json?raw";
// import.meta.resolve does not check existence; this import fails if the
// installed package stops shipping the schema.
import "@biomejs/biome/configuration_schema.json?raw";

it("biome.json $schema resolves to the installed @biomejs/biome schema", () => {
  const { $schema } = JSON.parse(biomeJson);
  const configUrl = new URL("../biome.json", import.meta.url);
  expect(new URL($schema, configUrl).href).toBe(
    import.meta.resolve("@biomejs/biome/configuration_schema.json"),
  );
});
