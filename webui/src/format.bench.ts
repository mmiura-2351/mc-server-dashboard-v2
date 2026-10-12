import { describe, test } from "vitest";
import { heartbeatAge, humanizeBytes, shortId, statusPill } from "./format.ts";
import { t } from "./i18n/index.ts";

describe("format", () => {
  test("humanizeBytes", async ({ bench }) => {
    await bench("humanizeBytes", () => {
      humanizeBytes(1610612736);
    }).run();
  });

  test("shortId", async ({ bench }) => {
    await bench("shortId", () => {
      shortId("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee");
    }).run();
  });

  test("statusPill", async ({ bench }) => {
    await bench("statusPill", () => {
      statusPill("online");
      statusPill("draining");
      statusPill("offline");
    }).run();
  });

  test("heartbeatAge", async ({ bench }) => {
    await bench("heartbeatAge", () => {
      heartbeatAge(new Date(Date.now() - 45000).toISOString(), t);
    }).run();
  });
});
