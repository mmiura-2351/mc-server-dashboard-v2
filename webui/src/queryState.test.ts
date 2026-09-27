// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup.
import { describe, expect, it } from "vitest";
import { classifyQueryResult } from "./queryState.ts";

describe("classifyQueryResult", () => {
  it("classifies an initial pending result", () => {
    expect(classifyQueryResult({ data: undefined, isError: false })).toEqual({
      kind: "pending",
    });
  });

  it("classifies an initial error with no data", () => {
    expect(classifyQueryResult({ data: undefined, isError: true })).toEqual({
      kind: "error",
    });
  });

  it("classifies available data, including an empty result", () => {
    expect(classifyQueryResult({ data: [], isError: false })).toEqual({
      kind: "data",
      data: [],
    });
  });

  it("keeps cached data available through a background error and later success", () => {
    expect(classifyQueryResult({ data: ["cached"], isError: true })).toEqual({
      kind: "data",
      data: ["cached"],
    });
    expect(classifyQueryResult({ data: ["fresh"], isError: false })).toEqual({
      kind: "data",
      data: ["fresh"],
    });
  });
});
