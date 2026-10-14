/** Shared presentation mechanics contain no setup or browser workflow. */
export function focusTransition(heading, deliberate) {
  if (!deliberate) return;
  heading.focus({ preventScroll: true });
  heading.scrollIntoView({ behavior: 'instant', block: 'start' });
}

export function setStatus(element, text) {
  // Avoid repeating an unchanged live-region announcement on pushed snapshots.
  if (element.textContent !== text) element.textContent = text;
}

export function applyTheme(document, storage = globalThis.localStorage) {
  try {
    const selected = storage.getItem('mui-mode');
    if (selected === 'light' || selected === 'dark') document.documentElement.dataset.colorScheme = selected;
  } catch { /* The system theme remains available when local storage is denied. */ }
}
