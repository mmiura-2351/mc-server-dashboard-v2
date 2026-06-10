import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/dom";

// Under parallel agent worktrees (shared box, load average 20+) the default
// 1 s async-util timeout causes spurious waitFor/findByRole failures.  30 s
// is still tight enough to catch genuine hangs while surviving CPU starvation.
configure({ asyncUtilTimeout: 30_000 });
