import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/dom";

// Allow async DOM assertions to survive CPU contention on shared development hosts.
configure({ asyncUtilTimeout: 30_000 });
