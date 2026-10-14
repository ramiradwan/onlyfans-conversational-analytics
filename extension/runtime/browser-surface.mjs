// The extension's own state as reported to Brain over an authenticated
// companion session (ADR 0045). It names no account, count, or identifier.
export const BROWSER_SURFACE_SCHEMA = 'ofca-browser-surface/v2';

export function browserSurface({ consent, legal, historyErrorCode = null }) {
  const mode = consent?.consent?.mode;
  const capture = mode === 'full' ? 'active'
    : mode === 'paused' && consent.consent.resume_mode === 'full' ? 'paused' : 'off';
  const siteAccess = consent?.phase === 'permission_required' || consent?.onlyfans_permission === false
    ? 'needs_approval'
    : 'granted';
  return {
    schema: BROWSER_SURFACE_SCHEMA,
    capture,
    site_access: siteAccess,
    history_permission: consent?.history_permission === true ? 'granted' : 'missing',
    legal_review_required: legal?.requires_reauthorization === true,
    history_error_code: ['identity_required', 'unsupported_revision', 'history_unavailable', 'history_dependency_wait', 'software_update_required'].includes(historyErrorCode)
      ? historyErrorCode : null,
  };
}

/** Pause is always accepted; resume goes through the consent controller's Legal checks. */
export function applyControl(consentController, action) {
  if (action === 'capture.pause') {
    return consentController.state?.mode === 'full' ? consentController.setMode('pause') : undefined;
  }
  if (action === 'capture.resume') {
    return consentController.state?.mode === 'paused' ? consentController.setMode('resume') : undefined;
  }
  return undefined;
}

/** Fixed browser destinations only; the page owns any permission prompt. */
export function openBrowserControl(action, { openSurface, openCreatorAccount }) {
  if (action === 'browser.open.creator') return openCreatorAccount();
  if (action === 'browser.open.history') return openSurface({ surface: 'options', section: 'history', presentation: 'window' });
  if (action === 'browser.open.setup' || action === 'browser.open.access') {
    return openSurface({ surface: 'setup', section: 'full', presentation: 'window' });
  }
  throw Error('browser_action_unavailable');
}

// Recompute on change events only. Coalesces bursts into one read.
export function createSurfaceReporter({ companion, readState, relevant = () => true }) {
  let scheduled = false;
  async function publish() {
    scheduled = false;
    try {
      companion.reportSurface(browserSurface(await readState()));
    } catch { /* The next change event retries. */ }
    await companion.ensureControl();
  }
  function changed() {
    // Preview and off modes have no companion session: skip their frequent
    // writes, but still close a control session left from a paused Full mode.
    if (!relevant()) { void Promise.resolve(companion.ensureControl()).catch(() => undefined); return; }
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => { void publish(); });
  }
  // Session admission is the authoritative opportunity to publish a surface
  // that may have been computed before a companion channel was available.
  companion.subscribe?.(() => changed());
  return Object.freeze({ changed });
}
