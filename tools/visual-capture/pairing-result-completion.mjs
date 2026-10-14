const terminalFeedback = Object.freeze({
  declined: 'Connection declined. Try again.',
  cancelled: 'Connection cancelled.',
  confirmed: 'Extension connected.',
});

// Only the driver's three explicit terminal responses use this completion
// assertion. A resolved fixture promise does not mean Chromium has applied the
// terminal container-query rule. The caller retains its native paint barrier.
export async function waitForPairingResultPresentation(page, outcome, timeoutMs = 2_000) {
  if (!Object.hasOwn(terminalFeedback, outcome)) throw new Error('Unsupported terminal pairing result: ' + outcome);
  const expectedFeedback = terminalFeedback[outcome];
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2_000) throw new Error('Invalid pairing result timeout');
  return page.evaluate(async ({ expectedFeedback, outcome, timeoutMs }) => {
    const control = window.__regionWatchControl;
    if (!control) throw new Error('Native pairing-result observation is unavailable');
    const start = control.boundary().nativeAt;
    while (true) {
      const owners = document.querySelectorAll('[data-journey-state="desktop.extension_pairing"]');
      const owner = owners.length === 1 ? owners[0] : null;
      const footer = owner?.querySelector('[data-browser-feedback]');
      const actions = footer?.children[1]?.children[0];
      const feedback = footer?.querySelector('[data-reserved-region="browser-feedback"] [role="status"]');
      const expectedHeight = 2.5 * Number.parseFloat(getComputedStyle(document.documentElement).fontSize);
      const minHeight = actions ? Number.parseFloat(getComputedStyle(actions).minHeight) : null;
      const height = actions?.getBoundingClientRect().height;
      const terminal = owner && !owner.hasAttribute('data-pairing-active')
        && owner.getAttribute('data-pairing-path') === 'desktop-task'
        && feedback?.textContent.trim() === expectedFeedback
        && !owner.querySelector('input[type="checkbox"]')
        && ![...owner.querySelectorAll('button')].some(button => ['Cancel', 'Confirm connection', "Codes don't match"].includes(button.textContent.trim()));
      const at = control.boundary().nativeAt;
      if (terminal && minHeight === expectedHeight && height === expectedHeight) {
        return { outcome, start, at, expectedHeight, minHeight, height };
      }
      if (at - start >= timeoutMs) throw new Error('Terminal pairing presentation did not complete: ' + outcome);
      // Poll this exact terminal DOM/style assertion with the already-captured
      // native scheduler. This is not generic geometry stability or a sleep.
      await control.settle();
    }
  }, { expectedFeedback, outcome, timeoutMs });
}
