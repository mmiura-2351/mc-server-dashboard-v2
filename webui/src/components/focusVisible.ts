// Whether the browser shows `element`'s focus (`:focus-visible`): focus that
// came from the keyboard, not focus a click or a tap left behind. In a module
// of its own so tests can stub it: happy-dom and jsdom match `:focus-visible`
// on any focused element.
export function isFocusVisible(element: Element): boolean {
  return element.matches(":focus-visible");
}
