const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const exact = (value, keys) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));

/** This callback requests navigation only. The destination authenticates normally. */
export function createNativeReturn({ fetch, location, status, retry, extensionId,
  focus = { hidden: true },
  storage = globalThis.sessionStorage, now = Date.now,
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
    retry.hidden = true;
    focus.hidden = true;
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
        if (context && exact(context, ['state', 'journey_id']) && context.state === 'selected' && UUID.test(context.journey_id)) {
          journey = context.journey_id;
        } else {
          if (!configured && (!exact(context, ['state', 'csrf_token', 'entry_id']) || context.state !== 'select_workspace'
            || !UUID.test(context.entry_id) || !/^[A-Za-z0-9_-]{43}$/u.test(context.csrf_token) || selectionUnknown)) throw new Error('Entry unconfirmed');
          if (!configured) {
            const saved = storage?.getItem('native_workspace_selection_v1');
            if (saved) {
              const previous = JSON.parse(saved);
              if (previous.entry_id === context.entry_id) throw new Error('Entry unconfirmed');
            }
          }
          if (!journey && runtime?.sendMessage && /^[a-p]{32}$/u.test(extensionId)) {
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
          if (configured) {
            if (!journey) { finished = true; location.replace('/'); return true; }
          } else {
            // Only this same-origin request sees the bootstrap entry's CSRF.
            // Unknown selection is reconciled once by read, never mutation replay.
            selectionUnknown = true;
            if (!storage) throw new Error('Entry unconfirmed');
            storage.setItem('native_workspace_selection_v1', JSON.stringify({ entry_id: context.entry_id,
              journey_id: journey || null, expires_at: now() + 300_000 }));
            try {
              context = await entryJson(await bounded(fetch('/api/v1/provisioning/native-entry', {
                method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
                headers: { 'Content-Type': 'application/json', 'X-Provisioning-CSRF': context.csrf_token },
                body: JSON.stringify({ journey_id: journey || null }),
              })));
            } catch {
              context = await entryJson(await entryRead());
            }
            if (!exact(context, ['state', 'journey_id']) || context.state !== 'selected'
              || !UUID.test(context.journey_id) || (journey && context.journey_id !== journey)) throw new Error('Entry unconfirmed');
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
      if (!runtime?.sendMessage || !/^[a-p]{32}$/u.test(extensionId)) { fallback(); return true; }
      const result = await bounded(runtime.sendMessage(extensionId,
        { type: 'ofca.workspace.launch-return.v1', journey_id: journey, route }));
      if (current !== generation) return false;
      if (exact(result, ['ok', 'result']) && result.ok === true
        && exact(result.result, ['status']) && result.result.status === 'returned') {
        finished = true;
        status.textContent = 'Continue in your setup tab. You can close this tab.';
        try { close(); } catch { /* The confirmed owner remains the place to continue. */ }
        return true;
      }
      if (exact(result, ['ok', 'code']) && result.ok === false) {
        if (result.code === 'no_workspace') { fallback(); return true; }
        if (result.code === 'workspace_exists') {
          status.textContent = 'Continue in your existing setup tab.';
          focus.hidden = false;
          return false;
        }
      }
      throw new Error('Return unconfirmed');
    } catch {
      if (current !== generation) return false;
      status.textContent = 'Check your setup tab.';
      retry.hidden = false;
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
