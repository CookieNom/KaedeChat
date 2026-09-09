/** Wrap focus at a modal's boundaries without changing its dismissal policy. */
export function trapDialogFocus(event: KeyboardEvent, element: HTMLElement, selector: string) {
  const focusable = Array.from(element.querySelectorAll<HTMLElement>(selector));
  if (!focusable.length) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable.at(-1) ?? first;
  if (!element.contains(document.activeElement)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
