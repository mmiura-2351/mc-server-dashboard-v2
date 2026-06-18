import { describe, expect, it, vi } from "vitest";

// Capture the factory that lazyRetry passes to React.lazy so we can invoke it
// directly and verify the retry logic without a full render cycle.
let capturedFactory: () => Promise<{ default: unknown }>;

vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    lazy: (factory: () => Promise<{ default: unknown }>) => {
      capturedFactory = factory;
      return { $$typeof: Symbol.for("react.lazy") };
    },
  };
});

// Import after the mock is set up.
const { lazyRetry } = await import("./lazyRetry.ts");

describe("lazyRetry", () => {
  it("resolves on first attempt when the import succeeds", async () => {
    const Comp = () => null;
    const factory = vi.fn().mockResolvedValue({ default: Comp });

    lazyRetry(factory);

    const result = await capturedFactory();
    expect(result.default).toBe(Comp);
    expect(factory).toHaveBeenCalledTimes(1);
  });

  it("retries once on failure then resolves", async () => {
    const Comp = () => null;
    const factory = vi
      .fn()
      .mockRejectedValueOnce(new Error("network error"))
      .mockResolvedValueOnce({ default: Comp });

    lazyRetry(factory);

    const result = await capturedFactory();
    expect(result.default).toBe(Comp);
    expect(factory).toHaveBeenCalledTimes(2);
  });

  it("rejects when both attempts fail", async () => {
    const factory = vi
      .fn()
      .mockRejectedValueOnce(new Error("first"))
      .mockRejectedValueOnce(new Error("second"));

    lazyRetry(factory);

    await expect(capturedFactory()).rejects.toThrow("second");
    expect(factory).toHaveBeenCalledTimes(2);
  });
});
