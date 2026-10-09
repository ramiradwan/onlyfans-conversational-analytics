const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const exact = (value, keys) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));

/** This callback requests navigation only. The destination authenticates normally. */
export function createNativeReturn({ fetch, location, status, retry, extensionId,
  focus = { hidden: true },
  storage = globalThis.sessionStorage, now = Date.now,
  loadLocalWorkspace = () => import('./native-workspace.mjs'),
  loadEntryParser = () => import('/provisioning/native-json.mjs'),
  runtime = globalThis.chrome?.runtime, close = () => globalThis.close(), timeoutMs = 10_000,
  loadValidation = () => Promise.all([import('./provisioning.js'), import('/provisioning/onboarding/json.mjs')]) }) {
  let pending = false;
  let finished = false;
  let generation = 0;
  let activeController;
  let selectionUnknown = false;
  const run = async () => {
    if (pending || finished) return false;
    pending = true;
    const current = ++generation;
    let failureMessage = 'Setup could not be opened.';
    let closedRefusal = false;
    retry.hidden = true;
    focus.hidden = true;
    status.textContent = 'Continuing setup…';
    const controller = new AbortController();
    activeController = controller;
    const deadline = setTimeout(() => controller.abort(), timeoutMs);
    const bounded = (operation) => Promise.race([operation, new Promise((_, reject) => {
      if (controller.signal.aborted) { reject(new Error('Return unconfirmed')); return; }
      controller.signal.addEventListener('abort', () => reject(new Error('Return unconfirmed')), { once: true });
    })]);
    try {
      let journey = location.hash.startsWith('#journey=') ? location.hash.slice(9) : '';
      if ((location.hash && !UUID.test(journey)) || location.search || location.pathname !== '/provisioning/native-return'
        || location.origin !== 'http://bridge.localhost:17871') throw new Error('Invalid return');
      let recovery = null;
      let localRecovery = false;
      let savedContinuation = false;
      let restoredRecovery = false;
      {
        const entryRead = () => bounded(fetch('/api/v1/provisioning/native-entry', {
          credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
          headers: { Accept: 'application/json' },
        }));
        const entryJson = async (reply) => {
          if (!reply.ok || reply.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') throw new Error('Entry unavailable');
          const text = await bounded(reply.text());
          if (text.length > 1024) throw new Error('Invalid entry');
          const { parseOnboardingJson } = await bounded(loadEntryParser());
          return parseOnboardingJson(text);
        };
        const entry = await entryRead();
        if (entry.status !== 401 || !journey) {
        const configured = entry.status === 404;
        let context = configured ? null : await entryJson(entry);
        const savedText = storage?.getItem('native_workspace_selection_v1');
        const saved = savedText ? JSON.parse(savedText) : null;
        const selectedRecovery = (value) => exact(value, ['state', 'journey_id', 'previous_journey_id'])
          && value.state === 'selected' && UUID.test(value.journey_id) && UUID.test(value.previous_journey_id);
        const savedRecovery = () => (exact(saved, ['entry_id', 'journey_id', 'expires_at', 'recovery_id']) && UUID.test(saved.recovery_id)
          || exact(saved, ['entry_id', 'journey_id', 'expires_at', 'local_recovery']) && saved.local_recovery === true
          || saved?.continue_saved === true && (exact(saved, ['entry_id', 'journey_id', 'expires_at', 'continue_saved'])
            || exact(saved, ['entry_id', 'journey_id', 'expires_at', 'continue_saved', 'recovery_id']) && UUID.test(saved.recovery_id)))
          && UUID.test(saved.entry_id) && UUID.test(saved.journey_id)
          && Number.isSafeInteger(saved.expires_at) && saved.expires_at > now() && saved.expires_at <= now() + 300_000;
        if (selectedRecovery(context)) {
          if (!savedRecovery() || context.previous_journey_id !== saved.journey_id
            || (journey && journey !== context.previous_journey_id)) throw new Error('Entry unconfirmed');
          localRecovery = saved.local_recovery === true;
          savedContinuation = saved.continue_saved === true;
          recovery = { entry_id: saved.entry_id, previous_journey_id: saved.journey_id,
            ...(saved.recovery_id ? { recovery_id: saved.recovery_id } : {}) };
          restoredRecovery = savedContinuation && Boolean(saved.recovery_id);
          journey = context.journey_id;
        } else if (context && exact(context, ['state', 'journey_id']) && context.state === 'selected' && UUID.test(context.journey_id)) {
          if ((journey && journey !== context.journey_id) || saved?.recovery_id || saved?.local_recovery) throw new Error('Entry unconfirmed');
          if (saved?.continue_saved === true) {
            if (!exact(saved, ['entry_id', 'journey_id', 'expires_at', 'continue_saved'])
              || !UUID.test(saved.entry_id) || saved.journey_id !== null || !Number.isSafeInteger(saved.expires_at)
              || saved.expires_at <= now() || saved.expires_at > now() + 300_000) throw new Error('Entry unconfirmed');
            savedContinuation = true;
          }
          journey = context.journey_id;
        } else {
          const boundTarget = context?.target_journey_id;
          savedContinuation = context?.continue_saved === true;
          if (!configured && (!(exact(context, ['state', 'csrf_token', 'entry_id'])
            || savedContinuation && !journey && exact(context, ['state', 'csrf_token', 'entry_id', 'continue_saved'])
            || (exact(context, ['state', 'csrf_token', 'entry_id', 'target_journey_id'])
              || savedContinuation && exact(context, ['state', 'csrf_token', 'entry_id', 'target_journey_id', 'continue_saved']))
              && UUID.test(boundTarget) && boundTarget === journey)
            || context.state !== 'select_workspace'
            || !UUID.test(context.entry_id) || !/^[A-Za-z0-9_-]{43}$/u.test(context.csrf_token) || selectionUnknown)) throw new Error('Entry unconfirmed');
          if (!configured) {
            if (saved?.entry_id === context.entry_id) throw new Error('Entry unconfirmed');
          }
          if (!configured && runtime?.sendMessage && /^[a-p]{32}$/u.test(extensionId)) {
            const prepared = await bounded(runtime.sendMessage(extensionId,
              { type: savedContinuation ? 'ofca.workspace.saved-continuation-prepare.v1' : 'ofca.workspace.recovery-prepare.v1',
                entry_id: context.entry_id }));
            if (controller.signal.aborted || current !== generation) return false;
            if (exact(prepared, ['ok', 'result']) && prepared.ok === true) {
              const result = prepared.result;
              if (!savedContinuation && exact(result, ['status', 'journey_id']) && result.status === 'launch_pending' && UUID.test(result.journey_id)) {
                if (journey && journey !== result.journey_id) throw new Error('Entry unconfirmed');
                journey = result.journey_id;
              } else if (exact(result, ['status', 'recovery_id', 'previous_journey_id']) && result.status === 'recovery_ready'
                && UUID.test(result.recovery_id) && UUID.test(result.previous_journey_id)) {
                if (journey && journey !== result.previous_journey_id) throw new Error('Entry unconfirmed');
                recovery = { entry_id: context.entry_id, recovery_id: result.recovery_id, previous_journey_id: result.previous_journey_id };
                journey = result.previous_journey_id;
              } else if (!savedContinuation && exact(result, ['status']) && result.status === 'launch_expired') {
                finished = true; status.textContent = 'Continue in your setup tab. You can close this tab.';
                try { close(); } catch { /* The verified extension owner provides the next action. */ }
                return true;
              } else throw new Error('Entry unconfirmed');
            } else {
              if (!(exact(prepared, ['ok', 'code']) && prepared.ok === false && prepared.code === 'no_workspace')) throw new Error('Entry unconfirmed');
                if (boundTarget && !savedContinuation) localRecovery = true;
            }
          } else if (!configured && boundTarget && !savedContinuation) {
            localRecovery = true;
          } else if (!journey && runtime?.sendMessage && /^[a-p]{32}$/u.test(extensionId)) {
            const discovered = await bounded(runtime.sendMessage(extensionId, { type: 'ofca.workspace.launch-discover.v1' }));
            if (exact(discovered, ['ok', 'result']) && discovered.ok === true
              && exact(discovered.result, ['status', 'journey_id']) && discovered.result.status === 'pending_launch'
              && UUID.test(discovered.result.journey_id)) journey = discovered.result.journey_id;
            else if (exact(discovered, ['ok', 'code']) && discovered.ok === false && discovered.code === 'workspace_exists') {
              status.textContent = 'Continue in your existing setup tab.'; focus.hidden = false; return false;
            } else if (!(exact(discovered, ['ok', 'code']) && discovered.ok === false && discovered.code === 'no_workspace')) {
              throw new Error('Entry unconfirmed');
            }
          }
          if (controller.signal.aborted || current !== generation) return false;
          if (localRecovery) recovery = { entry_id: context.entry_id, previous_journey_id: boundTarget };
          if (savedContinuation && !recovery && boundTarget) recovery = { entry_id: context.entry_id, previous_journey_id: boundTarget };
          if (configured) {
            if (!journey) { finished = true; location.replace('/'); return true; }
          } else {
            // Only this same-origin request sees the bootstrap entry's CSRF.
            // Unknown selection is reconciled once by read, never mutation replay.
            selectionUnknown = true;
            if (!storage) throw new Error('Entry unconfirmed');
            storage.setItem('native_workspace_selection_v1', JSON.stringify({ entry_id: context.entry_id,
              journey_id: journey || null, expires_at: now() + 300_000,
              ...(savedContinuation ? { continue_saved: true, ...(recovery?.recovery_id ? { recovery_id: recovery.recovery_id } : {}) }
                : localRecovery ? { local_recovery: true } : recovery ? { recovery_id: recovery.recovery_id } : {}) }));
            const selectedEntryId = context.entry_id;
            try {
              const selectedReply = await bounded(fetch('/api/v1/provisioning/native-entry', {
                method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
                headers: { 'Content-Type': 'application/json', 'X-Provisioning-CSRF': context.csrf_token },
                body: JSON.stringify({ journey_id: journey || null,
                  ...(savedContinuation ? { continue_saved: true } : recovery ? { recover: true } : {}) }),
              }));
              if ((recovery || savedContinuation) && selectedReply.status === 409) {
                failureMessage = 'Setup could not be recovered.';
                closedRefusal = true; finished = true;
              }
              context = await entryJson(selectedReply);
            } catch {
              context = await entryJson(await entryRead());
            }
            if (failureMessage === 'Setup could not be recovered.' && context?.state === 'select_workspace'
              && context.entry_id === selectedEntryId && (exact(context, ['state', 'csrf_token', 'entry_id'])
                || exact(context, ['state', 'csrf_token', 'entry_id', 'target_journey_id'])
                || context.continue_saved === true
                  && exact(context, ['state', 'csrf_token', 'entry_id', 'target_journey_id', 'continue_saved']))) {
              closedRefusal = true; finished = true;
            }
            if (recovery ? (!selectedRecovery(context) || context.previous_journey_id !== recovery.previous_journey_id)
              : (!exact(context, ['state', 'journey_id']) || context.state !== 'selected'
                || !UUID.test(context.journey_id) || (journey && context.journey_id !== journey))) throw new Error('Entry unconfirmed');
            journey = context.journey_id;
          }
        }
        if (controller.signal.aborted || current !== generation) return false;
        // Chrome's MessageSender.url retains the document's original URL after
        // replaceState. Keep manual entry bare; the closed request and live
        // pending launch intent bind its discovered journey without a reload.
        }
      }
      const read = (path) => bounded(fetch(path, { credentials: 'same-origin', cache: 'no-store',
        redirect: 'error', signal: controller.signal,
        headers: { Accept: 'application/json', 'X-Onboarding-Journey': journey } }));
      let route = 'provisioning';
      let response = await read('/api/v1/provisioning/state');
      if (recovery && response.status === 404) throw new Error('Return unconfirmed');
      if (response.status === 404) { route = 'bridge'; response = await read('/api/v1/onboarding/state'); }
      // Explicit authentication-required permits only the same navigation request.
      // Network errors, unknown state and HTML never establish a route or readiness.
      if (response.status !== 401) {
        if (!response.ok || response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json'
          || !response.headers.get('X-Onboarding-Capabilities')?.split(',').map((v) => v.trim()).includes('local-onboarding.v1')) {
          throw new Error('Return unconfirmed');
        }
        const text = await bounded(response.text());
        if (new TextEncoder().encode(text).byteLength > 4096) throw new Error('Invalid state');
        const [{ parseBrainOnboardingState }, { parseOnboardingJson }] = await bounded(loadValidation());
        const state = parseBrainOnboardingState(parseOnboardingJson(text));
        if (!state || state.kind !== 'snapshot' || state.journey_id !== journey) throw new Error('Invalid state');
      }
      if (controller.signal.aborted || current !== generation) return false;
      const fallback = () => { finished = true; location.replace(`${route === 'bridge' ? '/' : '/provisioning'}#journey=${journey}`); };
      if (savedContinuation && !recovery?.recovery_id) { fallback(); return true; }
      if (localRecovery) {
        const { returnToLocalWorkspace } = await bounded(loadLocalWorkspace());
        const result = await bounded(returnToLocalWorkspace({ fetch, location, storage,
          context: { ...recovery, journey_id: journey }, current: () => !controller.signal.aborted && current === generation }));
        if (controller.signal.aborted || current !== generation) return false;
        if (exact(result, ['status']) && result.status === 'continued') { fallback(); return true; }
        if (!exact(result, ['status']) || result.status !== 'returned') throw new Error('Return unconfirmed');
        finished = true; status.textContent = 'Continue in your setup tab. You can close this tab.';
        try { close(); } catch { /* The local document acknowledged its arrival. */ }
        return true;
      }
      if (!runtime?.sendMessage || !/^[a-p]{32}$/u.test(extensionId)) {
        if (recovery) throw new Error('Return unconfirmed');
        fallback(); return true;
      }
      if (restoredRecovery) {
        const reattached = await bounded(runtime.sendMessage(extensionId,
          { type: 'ofca.workspace.recovery-reattach.v1', ...recovery, journey_id: journey }));
        if (controller.signal.aborted || current !== generation) return false;
        if (!exact(reattached, ['ok', 'result']) || reattached.ok !== true
          || !exact(reattached.result, ['status']) || reattached.result.status !== 'reattached') throw new Error('Return unconfirmed');
      }
      const result = await bounded(runtime.sendMessage(extensionId,
        recovery ? { type: 'ofca.workspace.recovery-return.v1', ...recovery, journey_id: journey, route }
          : { type: 'ofca.workspace.launch-return.v1', journey_id: journey, route }));
      if (current !== generation) return false;
      if (exact(result, ['ok', 'result']) && result.ok === true
        && exact(result.result, ['status']) && (result.result.status === 'returned' || (recovery && result.result.status === 'continued'))) {
        finished = true;
        if (result.result.status === 'continued') return true;
        status.textContent = 'Continue in your setup tab. You can close this tab.';
        try { close(); } catch { /* The confirmed owner remains the place to continue. */ }
        return true;
      }
      if (exact(result, ['ok', 'code']) && result.ok === false) {
        if (!recovery && result.code === 'no_workspace') { fallback(); return true; }
        if (!recovery && result.code === 'workspace_exists') {
          status.textContent = 'Continue in your existing setup tab.';
          focus.hidden = false;
          return false;
        }
      }
      throw new Error('Return unconfirmed');
    } catch {
      if (current !== generation) return false;
      status.textContent = failureMessage;
      retry.hidden = closedRefusal;
      return false;
    } finally {
      clearTimeout(deadline);
      if (current === generation) pending = false;
    }
  };
  run.stop = () => { generation++; pending = false; activeController?.abort(); };
  return run;
}

if (typeof document !== 'undefined' && document.querySelector('main[data-native-return]')) {
  const retry = document.getElementById('native-return-retry');
  const focus = document.getElementById('native-return-focus');
  const extensionId = document.querySelector('main[data-native-return]').dataset.extensionId;
  const run = createNativeReturn({ fetch: globalThis.fetch.bind(globalThis), location: globalThis.location,
    status: document.getElementById('native-return-status'), retry, focus, extensionId });
  focus.addEventListener('click', async () => {
    focus.disabled = true;
    let timer;
    try { await Promise.race([globalThis.chrome?.runtime?.sendMessage(extensionId, { type: 'ofca.workspace.launch-focus.v1' }),
      new Promise((resolve) => { timer = setTimeout(resolve, 10_000); })]); }
    catch { /* Existing owner remains authoritative; no new setup is opened. */ }
    finally { clearTimeout(timer); focus.disabled = false; }
  });
  retry.addEventListener('click', () => { void run(); });
  globalThis.addEventListener('pagehide', run.stop);
  globalThis.addEventListener('pageshow', (event) => { if (event.persisted) void run(); });
  void run();
}
