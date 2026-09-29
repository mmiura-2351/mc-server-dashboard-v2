// @vitest-environment jsdom
// Pinned to jsdom: the copy tests drive the document.execCommand("copy")
// fallback, which is only exercised when navigator.clipboard is absent. jsdom
// omits it; happy-dom provides it, so the fallback would be skipped
// (issue #1751).
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { t } from "../i18n/index.ts";
import { ServerAddressBadges } from "./ServerAddressBadges.tsx";

const JAVA = "survival.relay.example.com";
const BEDROCK_NAME = `${t("dashboard.bedrockLabel")}: play.example.com:19132`;
const COPIED = t("dashboard.copiedJoinHostname");

function addresses(overrides: Record<string, unknown> = {}) {
  return {
    join_hostname: JAVA,
    bedrock_address: "play.example.com",
    bedrock_port: 19132,
    ...overrides,
  };
}

function renderBadges(overrides: Record<string, unknown> = {}) {
  return render(
    <ServerAddressBadges
      server={addresses(overrides)}
      buttonClassName="badge copyable"
      fallback={<span>:25565</span>}
    />,
  );
}

// The value handed to the clipboard fallback textarea on each copy, and the
// execCommand result the next copies report.
let copiedTexts: string[];
let copySucceeds: boolean;

// Click a badge and flush the clipboard promise's settlement.
async function click(element: HTMLElement) {
  await act(async () => {
    fireEvent.click(element);
  });
}

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

function javaButton() {
  return screen.getByRole("button", { name: JAVA });
}

function bedrockButton() {
  return screen.getByRole("button", { name: BEDROCK_NAME });
}

beforeEach(() => {
  vi.useFakeTimers();
  copiedTexts = [];
  copySucceeds = true;
  // jsdom does not define execCommand; define it so vi.spyOn can wrap it.
  if (!("execCommand" in document)) {
    Object.defineProperty(document, "execCommand", {
      value: () => true,
      writable: true,
      configurable: true,
    });
  }
  vi.spyOn(document, "execCommand").mockImplementation((command) => {
    if (command === "copy") {
      const areas = document.querySelectorAll("textarea");
      copiedTexts.push(areas[areas.length - 1]?.value ?? "");
    }
    return copySucceeds;
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("ServerAddressBadges display", () => {
  it("shows the Java join hostname alone as a native copy button", () => {
    renderBadges();

    const java = javaButton();
    expect(java.tagName).toBe("BUTTON");
    expect(java).toHaveAttribute("type", "button");
    expect(java.textContent).toBe(JAVA);
    expect(java).toHaveAttribute("title", JAVA);
    // The fallback belongs to servers without a join hostname only.
    expect(screen.queryByText(":25565")).not.toBeInTheDocument();
  });

  it("shows the fallback and no Java button without a join hostname", () => {
    renderBadges({ join_hostname: null, bedrock_port: null });

    expect(screen.getByText(":25565")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows the Bedrock address:port with a host-only copy tooltip", () => {
    renderBadges();

    const bedrock = bedrockButton();
    expect(bedrock).toHaveAttribute("type", "button");
    expect(bedrock).toHaveAttribute(
      "title",
      t("dashboard.bedrockAddressCopyTitle", { port: 19132 }),
    );
    // Java and Bedrock render side by side, Java first.
    expect(screen.getAllByRole("button")).toEqual([javaButton(), bedrock]);
  });

  it("hides the Bedrock button when the server has no Bedrock port", () => {
    renderBadges({ bedrock_port: null });

    expect(javaButton()).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: new RegExp(t("dashboard.bedrockLabel")),
      }),
    ).not.toBeInTheDocument();
  });

  it("applies the placement's class and style to both buttons", () => {
    render(
      <ServerAddressBadges
        server={addresses()}
        buttonClassName="copyable"
        buttonStyle={{ padding: 0 }}
        fallback={null}
      />,
    );

    for (const button of [javaButton(), bedrockButton()]) {
      expect(button).toHaveAttribute("class", "copyable");
      expect(button).toHaveStyle({ padding: "0px" });
    }
  });
});

describe.each([
  {
    kind: "Java",
    button: javaButton,
    copied: t("dashboard.copiedJoinHostname"),
    // The join hostname is the whole Java address.
    copiedText: JAVA,
  },
  {
    kind: "Bedrock",
    button: bedrockButton,
    copied: t("dashboard.copiedBedrockAddress"),
    // Host only: Bedrock's "Add Server" screen has a separate Port field.
    copiedText: "play.example.com",
  },
])("ServerAddressBadges $kind copy", ({ button, copied, copiedText }) => {
  it("copies the address and shows the copied label for exactly 1500 ms", async () => {
    renderBadges();
    const target = button();

    await click(target);

    expect(copiedTexts).toEqual([copiedText]);
    expect(target).toHaveTextContent(copied);
    advance(1499);
    expect(target).toHaveTextContent(copied);
    advance(1);
    expect(target).toBe(button());
  });

  it("restarts the 1500 ms reset on a repeated successful copy", async () => {
    renderBadges();
    const target = button();

    await click(target);
    advance(1000);
    await click(target);
    advance(1499);
    expect(target).toHaveTextContent(copied);
    advance(1);
    expect(target).toBe(button());
  });

  it("keeps showing the address when the copy fails", async () => {
    copySucceeds = false;
    renderBadges();
    const target = button();

    await click(target);

    expect(copiedTexts).toEqual([copiedText]);
    expect(target).toBe(button());
  });

  it("drops the copied label at once when a re-click fails (issue #976)", async () => {
    renderBadges();
    const target = button();
    await click(target);
    expect(target).toHaveTextContent(copied);

    copySucceeds = false;
    await click(target);

    // Reverted immediately, not by the pending success timer.
    expect(target).toBe(button());
  });
});

describe("ServerAddressBadges independent copy state", () => {
  it("flips only the clicked button to the copied label", async () => {
    renderBadges();

    await click(bedrockButton());
    expect(screen.getAllByText(COPIED)).toHaveLength(1);
    expect(javaButton()).toBeInTheDocument();

    advance(1500);
    await click(javaButton());
    expect(screen.getAllByText(COPIED)).toHaveLength(1);
    expect(bedrockButton()).toBeInTheDocument();
  });

  it("resets each button on its own timer", async () => {
    renderBadges();
    const java = javaButton();

    await click(java);
    advance(1000);
    await click(bedrockButton());
    advance(500);

    // Java's timer has fired; Bedrock's, started 1000 ms later, has not.
    expect(java).toBe(javaButton());
    expect(screen.getAllByText(COPIED)).toHaveLength(1);
    advance(1000);
    expect(screen.queryByText(COPIED)).not.toBeInTheDocument();
  });
});
