// Whether `event` is a key the input method takes while it composes text, as
// an Escape that cancels a conversion. `isComposing` is the standard signal;
// keyCode 229 is the one some browsers send instead, as Safari does for the
// key that ends a composition. A handler leaves such a key alone and does not
// prevent it, so the IME still gets it (#3993).
export function isComposing(event: KeyboardEvent): boolean {
  return event.isComposing || event.keyCode === 229;
}
