import {
  QueryClient,
  QueryClientProvider,
  useMutation,
} from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { type ReactNode, startTransition, useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Modal } from "./Modal.tsx";
import { ToastProvider, useToast } from "./Toast.tsx";

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      <Modal open={open} title="Title" onClose={() => setOpen(false)}>
        <button type="button">Inside</button>
      </Modal>
    </>
  );
}

describe("Modal", () => {
  it("renders nothing when closed", () => {
    render(
      <Modal open={false} title="Title" onClose={() => {}}>
        <p>Body</p>
      </Modal>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders the title and body when open", () => {
    render(
      <Modal open={true} title="Settings" onClose={() => {}}>
        <p>Body content</p>
      </Modal>,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Settings");
    expect(dialog).toHaveTextContent("Body content");
  });

  it("calls onClose when the backdrop is clicked", () => {
    const onClose = vi.fn();
    render(
      <Modal open={true} title="Title" onClose={onClose}>
        <p>Body</p>
      </Modal>,
    );
    fireEvent.click(screen.getByTestId("modal-backdrop"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when Escape is pressed without tabbing into the dialog", () => {
    const onClose = vi.fn();
    render(
      <Modal open={true} title="Title" onClose={onClose}>
        <p>Body</p>
      </Modal>,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // So a page listener that runs after it, once the dialog has closed, can
  // still tell the Escape was the dialog's (#3920).
  it("marks the Escape that closes it as handled", () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        <p>Body</p>
      </Modal>,
    );
    expect(fireEvent.keyDown(document, { key: "Escape" })).toBe(false);
  });

  // An Escape that cancels an IME conversion in a dialog field is the IME's,
  // so the dialog and what was typed stay (#3993).
  it.each([{ isComposing: true }, { keyCode: 229 }])(
    "leaves an Escape the IME is composing with to it (%o)",
    (composing) => {
      const onClose = vi.fn();
      render(
        <Modal open={true} title="Title" onClose={onClose}>
          <input aria-label="Name" />
        </Modal>,
      );
      const input = screen.getByRole("textbox", { name: "Name" });

      const notPrevented = fireEvent.keyDown(input, {
        key: "Escape",
        ...composing,
      });

      expect(onClose).not.toHaveBeenCalled();
      expect(notPrevented).toBe(true);
    },
  );

  it("does not call onClose when the dialog body is clicked", () => {
    const onClose = vi.fn();
    render(
      <Modal open={true} title="Title" onClose={onClose}>
        <p>Body</p>
      </Modal>,
    );
    fireEvent.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("restores focus to the triggering element when closed", () => {
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Open" });
    trigger.focus();
    fireEvent.click(trigger);

    // Focus has moved into the dialog while open.
    expect(screen.getByRole("dialog")).toContainElement(
      document.activeElement as HTMLElement,
    );

    fireEvent.keyDown(document, { key: "Escape" });

    // On close, focus returns to the element that opened the dialog.
    expect(document.activeElement).toBe(trigger);
  });

  it("traps Tab focus within the open dialog (wraps last → first)", () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        <button type="button">First</button>
        <button type="button">Last</button>
      </Modal>,
    );
    const first = screen.getByRole("button", { name: "First" });
    const last = screen.getByRole("button", { name: "Last" });

    last.focus();
    // Cancelled, or the browser would move focus one step further.
    expect(fireEvent.keyDown(document, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(first);
  });

  it("traps Shift+Tab focus within the open dialog (wraps first → last)", () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        <button type="button">First</button>
        <button type="button">Last</button>
      </Modal>,
    );
    const first = screen.getByRole("button", { name: "First" });
    const last = screen.getByRole("button", { name: "Last" });

    first.focus();
    expect(fireEvent.keyDown(document, { key: "Tab", shiftKey: true })).toBe(
      false,
    );
    expect(document.activeElement).toBe(last);
  });

  it("Escape closes only the topmost of stacked dialogs (Files-drawer shape)", () => {
    const onCloseOuter = vi.fn();
    const onCloseInner = vi.fn();
    render(
      <>
        <Modal open={true} title="History" onClose={onCloseOuter}>
          <button type="button">Outer body</button>
        </Modal>
        <Modal open={true} title="Confirm rollback" onClose={onCloseInner}>
          <button type="button">Inner body</button>
        </Modal>
      </>,
    );

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCloseInner).toHaveBeenCalledTimes(1);
    expect(onCloseOuter).not.toHaveBeenCalled();
  });
});

// Puts focus on the page body, where a browser leaves it after a click on
// text that takes no focus, or after the focused control is removed.
function focusBody() {
  (document.activeElement as HTMLElement).blur();
  expect(document.activeElement).toBe(document.body);
}

// Presses Tab where focus is and lets the browser run it: the test DOM moves
// no focus, so `landsOn` stands for where the browser's Tab lands. The key
// comes up once that move, and anything it set off, has run.
async function browserTab(landsOn: HTMLElement, shiftKey = false) {
  const from = document.activeElement ?? document.body;
  expect(fireEvent.keyDown(from, { key: "Tab", shiftKey })).toBe(true);
  landsOn.focus();
  await settle();
  fireEvent.keyUp(document.activeElement ?? document.body, {
    key: "Tab",
    shiftKey,
  });
}

const twoControls = (
  <>
    <button type="button">First</button>
    <button type="button">Last</button>
  </>
);

// The Files-drawer shape, with the confirm opened over the drawer, and the
// page behind them.
const stackedDialogs = (
  <>
    <button type="button">Behind</button>
    <Modal open={true} title="History" onClose={() => {}}>
      <button type="button">Outer body</button>
    </Modal>
    <Modal open={true} title="Confirm rollback" onClose={() => {}}>
      <button type="button">Inner first</button>
      <button type="button">Inner last</button>
    </Modal>
  </>
);

// The dialog hears about some changes on a microtask.
async function settle() {
  await act(async () => {});
}

// How a confirm closes its dialog and focuses the control that opened its
// menu, as ServerDetailPage's force-stop confirm does in a click handler.
type CloseOrder = (steps: {
  close: () => void;
  focusStop: () => void;
  mutate: (onSuccess: () => void) => void;
}) => void;

// The element that opened the dialog is gone by the time it closes, so the
// dialog's own focus restore does nothing and the confirm's focus stands.
function CloseAndFocusStop({ order }: { order: CloseOrder }) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(false);
  const stopRef = useRef<HTMLButtonElement>(null);
  const mutation = useMutation({ mutationFn: async () => {} });
  return (
    <>
      <button type="button" ref={stopRef}>
        Stop
      </button>
      {!done && (
        <button type="button" onClick={() => setOpen(true)}>
          Open
        </button>
      )}
      <Modal open={open} title="Title" onClose={() => setOpen(false)}>
        <button
          type="button"
          onClick={() =>
            order({
              close: () => {
                setOpen(false);
                setDone(true);
              },
              focusStop: () => stopRef.current?.focus(),
              mutate: (onSuccess) => mutation.mutate(undefined, { onSuccess }),
            })
          }
        >
          Confirm
        </button>
      </Modal>
    </>
  );
}

// A field whose blur opens a second dialog over the first, beside it.
function BlurOpensSibling() {
  const [upper, setUpper] = useState(false);
  return (
    <>
      <Modal open={true} title="Lower" onClose={() => {}}>
        <button type="button">Lower first</button>
        <input aria-label="Field" onBlur={() => setUpper(true)} />
        <button type="button">Lower last</button>
      </Modal>
      <Modal open={upper} title="Upper" onClose={() => {}}>
        <button type="button">Upper first</button>
      </Modal>
    </>
  );
}

// While a modal is open, a Tab that lands focus outside the topmost dialog and
// the toast stack is pulled back into the dialog, so Tab never walks the page
// behind. Tab from the page body is the browser's, and focus a script moves
// is left where it goes until the next Tab (WEBUI_SPEC.md 7.4).
describe("Modal focus outside the dialog", () => {
  function renderOverPage(body: ReactNode = twoControls) {
    render(
      <>
        <button type="button">Behind</button>
        <Modal open={true} title="Title" onClose={() => {}}>
          {body}
        </Modal>
      </>,
    );
    return screen.getByRole("button", { name: "Behind" });
  }

  const first = () => screen.getByRole("button", { name: "First" });
  const last = () => screen.getByRole("button", { name: "Last" });

  // After a focused control is removed, the browser's Tab goes on from where
  // that control was, which the trap cannot know.
  it("leaves Tab from the page body to the browser", () => {
    renderOverPage();
    focusBody();

    expect(fireEvent.keyDown(document.body, { key: "Tab" })).toBe(true);
    expect(document.activeElement).toBe(document.body);
  });

  it("pulls a Tab that lands on the page behind back to the dialog's first control", async () => {
    const behind = renderOverPage();
    focusBody();

    await browserTab(behind);

    expect(document.activeElement).toBe(first());
  });

  it("pulls a Shift+Tab that lands behind back to the dialog's last control", async () => {
    const behind = renderOverPage();
    focusBody();

    await browserTab(behind, true);

    expect(document.activeElement).toBe(last());
  });

  it("pulls back a Tab that leaves the dialog from inside it", async () => {
    const behind = renderOverPage();
    first().focus();

    await browserTab(behind);

    expect(document.activeElement).toBe(first());
  });

  // Focus moves into a dialog's own scroller without `preventScroll`; only
  // the page's scroll is put back.
  it("undoes the page scroll of a Tab that landed behind the dialog", async () => {
    const behind = renderOverPage();
    focusBody();
    window.scrollTo(0, 0);
    const focus = vi.spyOn(first(), "focus");

    expect(fireEvent.keyDown(document.body, { key: "Tab" })).toBe(true);
    // The browser scrolls to where its Tab lands.
    window.scrollTo(0, 1204);
    behind.focus();
    await settle();

    expect(document.activeElement).toBe(first());
    expect(focus).not.toHaveBeenCalledWith({ preventScroll: true });
    expect(window.scrollY).toBe(0);
  });

  it("pulls a Tab that lands behind back to the dialog itself when it has no controls", async () => {
    const behind = renderOverPage(<p>Body</p>);
    focusBody();

    await browserTab(behind);

    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });

  it("pulls a Tab that lands in a dialog below into the topmost one", async () => {
    render(stackedDialogs);
    focusBody();

    await browserTab(screen.getByRole("button", { name: "Outer body" }));

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Inner first" }),
    );
  });

  it("leaves a Tab within the topmost of sibling dialogs to it", async () => {
    render(stackedDialogs);
    // Stubbed so a pull into the dialog below is seen, and cannot bounce.
    const outerPulls = [
      vi.spyOn(screen.getByRole("button", { name: "Outer body" }), "focus"),
      vi.spyOn(screen.getByRole("dialog", { name: "History" }), "focus"),
    ].map((spy) => spy.mockImplementation(() => {}));
    screen.getByRole("button", { name: "Inner first" }).focus();

    await browserTab(screen.getByRole("button", { name: "Inner last" }));

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Inner last" }),
    );
    for (const pull of outerPulls) {
      expect(pull).not.toHaveBeenCalled();
    }
  });

  // A confirm pressed while the Tab that reached it is still down: the Tab
  // counts only until its own focus move.
  it("leaves focus a script moves behind once the Tab has moved focus, key still down", () => {
    const behind = renderOverPage();
    first().focus();

    expect(fireEvent.keyDown(first(), { key: "Tab" })).toBe(true);
    // Where the browser's Tab lands, inside the dialog.
    last().focus();
    // The confirm's focus, before the Tab's keyup or timer.
    behind.focus();

    expect(document.activeElement).toBe(behind);
  });

  // The field's blur, at the start of the Tab's move, opens the upper dialog,
  // which takes focus before the Tab lands.
  it("leaves focus in a dialog that a blur opens over this one during a Tab", () => {
    render(<BlurOpensSibling />);
    const field = screen.getByRole("textbox", { name: "Field" });
    field.focus();

    expect(fireEvent.keyDown(field, { key: "Tab" })).toBe(true);
    act(() => field.blur());

    expect(document.activeElement).toBe(
      screen.getByRole("dialog", { name: "Upper" }),
    );
  });

  it("leaves focus that a script moves behind the dialog there", async () => {
    const behind = renderOverPage();

    behind.focus();
    await settle();

    expect(document.activeElement).toBe(behind);
  });

  it("leaves focus that a script drops to the page body there", async () => {
    renderOverPage();

    last().focus();
    last().blur();
    await settle();

    expect(document.activeElement).toBe(document.body);
  });

  it("does not take a Tab's direction or scroll for focus a script moves later", async () => {
    const behind = renderOverPage();
    focusBody();
    window.scrollTo(0, 0);

    fireEvent.keyDown(document.body, { key: "Tab", shiftKey: true });
    await new Promise((resolve) => setTimeout(resolve));
    window.scrollTo(0, 300);
    behind.focus();
    await settle();

    expect(document.activeElement).toBe(behind);
    expect(window.scrollY).toBe(300);
  });

  it("does not take a Tab's direction or scroll once its key is up", async () => {
    const behind = renderOverPage();
    focusBody();
    window.scrollTo(0, 0);

    fireEvent.keyDown(document.body, { key: "Tab", shiftKey: true });
    fireEvent.keyUp(document.body, { key: "Tab", shiftKey: true });
    window.scrollTo(0, 300);
    behind.focus();
    await settle();

    expect(document.activeElement).toBe(behind);
    expect(window.scrollY).toBe(300);
  });

  // Each way a confirm can close its dialog and focus behind it ends there,
  // however React schedules the close.
  it.each<[string, CloseOrder]>([
    [
      "closing, then focusing, in the click handler",
      ({ close, focusStop }) => {
        close();
        focusStop();
      },
    ],
    [
      "focusing, then closing, in the click handler",
      ({ close, focusStop }) => {
        focusStop();
        close();
      },
    ],
    [
      "closing, then focusing, in a promise callback",
      ({ close, focusStop }) => {
        void Promise.resolve().then(() => {
          close();
          focusStop();
        });
      },
    ],
    [
      "closing, then focusing, from a timer",
      ({ close, focusStop }) => {
        setTimeout(() => {
          close();
          focusStop();
        });
      },
    ],
    [
      "closing, then focusing, in a mutation's onSuccess",
      ({ close, focusStop, mutate }) => {
        mutate(() => {
          close();
          focusStop();
        });
      },
    ],
    [
      "closing in a transition, then focusing",
      ({ close, focusStop }) => {
        startTransition(close);
        focusStop();
      },
    ],
  ])("leaves focus on what a confirm focuses when %s", async (_, order) => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <CloseAndFocusStop order={order} />
      </QueryClientProvider>,
    );
    const open = screen.getByRole("button", { name: "Open" });
    open.focus();
    fireEvent.click(open);

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    await settle();

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Stop" }),
    );
  });

  it("moves Tab from focus a script put behind the dialog into it", async () => {
    const behind = renderOverPage();
    behind.focus();
    await settle();

    expect(fireEvent.keyDown(behind, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(first());
  });

  it("moves Shift+Tab from a dialog below to the topmost one's last control, not a toast", async () => {
    render(
      <ToastProvider>
        {stackedDialogs}
        <RaiseErrors />
      </ToastProvider>,
    );
    act(() => raise("Failed"));
    const outer = screen.getByRole("button", { name: "Outer body" });
    outer.focus();
    await settle();

    expect(fireEvent.keyDown(outer, { key: "Tab", shiftKey: true })).toBe(
      false,
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Inner last" }),
    );
  });

  it("leaves Tab between the dialog's own controls to the browser", () => {
    renderOverPage();
    first().focus();

    expect(fireEvent.keyDown(first(), { key: "Tab" })).toBe(true);
    expect(document.activeElement).toBe(first());
  });
});

// A control can match the trap's selector and still refuse focus; a Tab the
// trap takes over still lands, on the next control or the dialog itself.
describe("Modal focus on controls that cannot take it", () => {
  it("skips a hidden input at the end of the dialog", () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        {twoControls}
        <input type="hidden" name="token" />
      </Modal>,
    );
    const last = screen.getByRole("button", { name: "Last" });
    last.focus();

    expect(fireEvent.keyDown(last, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "First" }),
    );
  });

  it("goes on to the next control when one refuses focus", () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        <button type="button">A</button>
        <button type="button">B</button>
        <button type="button">C</button>
      </Modal>,
    );
    vi.spyOn(screen.getByRole("button", { name: "A" }), "focus")
      // As a control under `display: none` does.
      .mockImplementation(() => {});
    const c = screen.getByRole("button", { name: "C" });
    c.focus();

    expect(fireEvent.keyDown(c, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "B" }),
    );
  });

  it("falls back to the dialog itself when no control takes focus", async () => {
    render(
      <>
        <button type="button">Behind</button>
        <Modal open={true} title="Title" onClose={() => {}}>
          {twoControls}
        </Modal>
      </>,
    );
    for (const name of ["First", "Last"]) {
      vi.spyOn(
        screen.getByRole("button", { name }),
        "focus",
      ).mockImplementation(() => {});
    }
    focusBody();

    await browserTab(screen.getByRole("button", { name: "Behind" }));

    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });
});

// Chromium makes a scroller a Tab stop while its content overflows and
// nothing in it takes Tab, as a `.modal-body` whose controls are all disabled
// (WEBUI_SPEC.md 7.4). The trap counts it among its edges, so Tab wraps where
// the browser's walk does.
describe("Modal focus on a scroller with no control in it", () => {
  // Gives `el` content that overflows it on one axis, which the test DOM,
  // laying nothing out, never has.
  function overflow(el: HTMLElement, axis: "x" | "y", value = "auto") {
    const [scroll, client] =
      axis === "y"
        ? ["scrollHeight", "clientHeight"]
        : ["scrollWidth", "clientWidth"];
    el.style[axis === "y" ? "overflowY" : "overflowX"] = value;
    Object.defineProperty(el, scroll, { configurable: true, value: 600 });
    Object.defineProperty(el, client, { configurable: true, value: 200 });
  }

  function renderDialog(body: ReactNode, footer?: ReactNode) {
    render(
      <Modal open={true} title="Title" onClose={() => {}} footer={footer}>
        {body}
      </Modal>,
    );
    return screen
      .getByRole("dialog")
      .querySelector(".modal-body") as HTMLElement;
  }

  const disabledRows = (
    <>
      <button type="button" disabled>
        Row 1
      </button>
      <button type="button" disabled>
        Row 2
      </button>
    </>
  );
  // Not "Close", the name the backdrop has.
  const doneButton = <button type="button">Done</button>;
  const done = () => screen.getByRole("button", { name: "Done" });

  it("wraps Tab from the last control to a scrolling body", () => {
    const body = renderDialog(disabledRows, doneButton);
    overflow(body, "y");
    done().focus();

    expect(fireEvent.keyDown(done(), { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(body);
  });

  it("wraps Shift+Tab from a scrolling body to the last control", () => {
    const body = renderDialog(disabledRows, doneButton);
    overflow(body, "y");
    body.focus();

    expect(fireEvent.keyDown(body, { key: "Tab", shiftKey: true })).toBe(false);
    expect(document.activeElement).toBe(done());
  });

  it("wraps Tab to a body that scrolls sideways only", () => {
    const body = renderDialog(disabledRows, doneButton);
    overflow(body, "x");
    done().focus();

    expect(fireEvent.keyDown(done(), { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(body);
  });

  it("leaves Tab from a dialog with no control to the browser, to reach a scrolling body", () => {
    overflow(renderDialog(disabledRows), "y");
    const dialog = screen.getByRole("dialog");
    expect(document.activeElement).toBe(dialog);

    expect(fireEvent.keyDown(dialog, { key: "Tab" })).toBe(true);
  });

  it("stops on a scroller nested in the body instead of the body", () => {
    const body = renderDialog(
      <div data-testid="inner">{disabledRows}</div>,
      doneButton,
    );
    overflow(body, "y");
    const inner = screen.getByTestId("inner");
    overflow(inner, "y");
    done().focus();

    expect(fireEvent.keyDown(done(), { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(inner);
  });

  it("does not stop on a scrolling body with a control in it", () => {
    const body = renderDialog(<button type="button">Row</button>, doneButton);
    overflow(body, "y");
    done().focus();

    expect(fireEvent.keyDown(done(), { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Row" }),
    );
  });

  it("does not stop on a scroller that tabindex -1 takes out of the Tab order", () => {
    renderDialog(
      <div data-testid="minus" tabIndex={-1}>
        {disabledRows}
      </div>,
      doneButton,
    );
    overflow(screen.getByTestId("minus"), "y");
    done().focus();

    expect(fireEvent.keyDown(done(), { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(done());
  });

  it("does not stop on a box that clips its overflow", () => {
    renderDialog(<div data-testid="clip">{disabledRows}</div>, doneButton);
    overflow(screen.getByTestId("clip"), "y", "hidden");
    done().focus();

    expect(fireEvent.keyDown(done(), { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(done());
  });

  // As a browser that does not stop Tab on the body runs past it; one that
  // takes focus from a script is still the stop it goes to (#3971).
  it("pulls a Shift+Tab the browser runs past a scrolling body, out of the dialog, back to the body", async () => {
    const body = renderDialog(disabledRows, doneButton);
    overflow(body, "y");
    done().focus();

    await browserTab(screen.getByTestId("modal-backdrop"), true);

    expect(document.activeElement).toBe(body);
  });
});

// A dialog that disables the control holding focus, as a confirm does while
// its action runs, keeps focus: it goes to the dialog, never to the page body
// (WEBUI_SPEC.md 7.4). The test DOM leaves focus on the disabled control;
// Chromium drops it to the body a frame later.
function BusyFooter() {
  const [busy, setBusy] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setBusy(true)}>
        Start elsewhere
      </button>
      <button type="button" disabled={busy} onClick={() => setBusy(true)}>
        Save
      </button>
    </>
  );
}

function HistoryWithConfirm() {
  const [confirming, setConfirming] = useState(false);
  return (
    <Modal open={true} title="History" onClose={() => {}}>
      <button type="button" onClick={() => setConfirming(true)}>
        Rollback
      </button>
      {confirming && (
        <Modal open={true} title="Confirm rollback" onClose={() => {}}>
          <BusyFooter />
        </Modal>
      )}
    </Modal>
  );
}

describe("Modal focus when its focused control is disabled", () => {
  it("moves focus to the dialog, unscrolled, when the focused control is disabled", async () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        <BusyFooter />
      </Modal>,
    );
    const dialog = screen.getByRole("dialog");
    const focus = vi.spyOn(dialog, "focus");
    const save = screen.getByRole("button", { name: "Save" });
    save.focus();

    fireEvent.click(save);
    await settle();

    expect(save).toBeDisabled();
    expect(document.activeElement).toBe(dialog);
    expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
  });

  it("leaves focus where it is when a control without it is disabled", async () => {
    render(
      <Modal open={true} title="Title" onClose={() => {}}>
        <BusyFooter />
      </Modal>,
    );
    const elsewhere = screen.getByRole("button", { name: "Start elsewhere" });
    elsewhere.focus();

    fireEvent.click(elsewhere);
    await settle();

    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(document.activeElement).toBe(elsewhere);
  });

  // The Files history drawer opens its rollback confirm inside its own body,
  // so the outer dialog sees the inner one's changes too.
  it("moves focus to the topmost dialog when a dialog nested inside another disables it", async () => {
    render(<HistoryWithConfirm />);
    fireEvent.click(screen.getByRole("button", { name: "Rollback" }));
    const save = screen.getByRole("button", { name: "Save" });
    save.focus();

    fireEvent.click(save);
    await settle();

    expect(document.activeElement).toBe(
      screen.getByRole("dialog", { name: "Confirm rollback" }),
    );
  });
});

// The toast stack stays reachable while a dialog is open (WEBUI_SPEC.md 7.4):
// Tab runs through the dialog, then the toasts, then wraps.
let raise: (message: string) => void;
function RaiseErrors() {
  const { showToast } = useToast();
  raise = (message) => showToast(message, "error");
  return null;
}

describe("Modal focus trap with toasts showing", () => {
  function renderWithToasts(body: ReactNode) {
    render(
      <ToastProvider>
        <RaiseErrors />
        <button type="button">Behind</button>
        <Modal open={true} title="Title" onClose={() => {}}>
          {body}
        </Modal>
      </ToastProvider>,
    );
    act(() => {
      raise("Failed 1");
      raise("Failed 2");
    });
  }

  // The modal's backdrop is a "Close" button too, so pick the toasts' own.
  function toastButtons(): HTMLElement[] {
    return screen
      .getAllByRole("alert")
      .map((toast) => within(toast).getByRole("button"));
  }

  // Presses Tab where focus is, and expects the trap to cancel it: at an
  // edge the browser would otherwise move focus one step further.
  function pressTab(shiftKey = false) {
    expect(
      fireEvent.keyDown(document.activeElement ?? document, {
        key: "Tab",
        shiftKey,
      }),
    ).toBe(false);
  }

  const controls = (
    <>
      <button type="button">First</button>
      <button type="button">Last</button>
    </>
  );

  it("moves Tab from the dialog's last control to the first toast", () => {
    renderWithToasts(controls);

    screen.getByRole("button", { name: "Last" }).focus();
    pressTab();

    expect(document.activeElement).toBe(toastButtons()[0]);
  });

  it("wraps Tab from the last toast to the dialog's first control", () => {
    renderWithToasts(controls);

    toastButtons()[1].focus();
    pressTab();

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "First" }),
    );
  });

  it("moves Shift+Tab from the dialog's first control to the last toast", () => {
    renderWithToasts(controls);

    screen.getByRole("button", { name: "First" }).focus();
    pressTab(true);

    expect(document.activeElement).toBe(toastButtons()[1]);
  });

  it("moves Shift+Tab from the dialog itself to the last toast", () => {
    renderWithToasts(controls);

    expect(document.activeElement).toBe(screen.getByRole("dialog"));
    pressTab(true);

    expect(document.activeElement).toBe(toastButtons()[1]);
  });

  it("moves Shift+Tab from the first toast to the dialog's last control", () => {
    renderWithToasts(controls);

    toastButtons()[0].focus();
    pressTab(true);

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Last" }),
    );
  });

  it("moves Tab from a dialog with no controls to the first toast", () => {
    renderWithToasts(<p>Body</p>);

    expect(document.activeElement).toBe(screen.getByRole("dialog"));
    pressTab();

    expect(document.activeElement).toBe(toastButtons()[0]);
  });

  it("wraps Tab from the last toast to the first when the dialog has no controls", () => {
    renderWithToasts(<p>Body</p>);

    toastButtons()[1].focus();
    pressTab();

    expect(document.activeElement).toBe(toastButtons()[0]);
  });

  it("leaves Tab between toasts to the browser", () => {
    renderWithToasts(controls);

    toastButtons()[0].focus();

    expect(fireEvent.keyDown(toastButtons()[0], { key: "Tab" })).toBe(true);
    expect(document.activeElement).toBe(toastButtons()[0]);
  });

  // In a browser a click on a toast's text leaves focus on the page body,
  // and the next Tab or Shift+Tab goes on from that text; the test DOM moves
  // neither, so both are done by hand.
  it("pulls Shift+Tab after a click on a toast's text back to the dialog's last control", async () => {
    renderWithToasts(controls);
    toastButtons()[0].focus();
    fireEvent.click(screen.getByText("Failed 1"));
    focusBody();

    // From the first toast's text the browser goes back to the page.
    await browserTab(screen.getByRole("button", { name: "Behind" }), true);

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Last" }),
    );
  });

  it("leaves Tab after a click on a toast's text to reach that toast", async () => {
    renderWithToasts(controls);
    fireEvent.click(screen.getByText("Failed 1"));
    focusBody();

    await browserTab(toastButtons()[0]);

    expect(document.activeElement).toBe(toastButtons()[0]);
  });

  it("pulls a Tab that lands behind back to a dialog with no controls, not to a toast", async () => {
    renderWithToasts(<p>Body</p>);
    focusBody();

    await browserTab(screen.getByRole("button", { name: "Behind" }));

    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });
});
