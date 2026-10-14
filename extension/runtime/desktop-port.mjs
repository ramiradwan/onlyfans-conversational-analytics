// The only pre-pairing browser port: one declared task in its exact helper tab.
import { LOCAL_SERVICE_ORIGIN } from '../transport/local-service-endpoints.mjs';
import { PORT, exact, isPayload, isObservation, helperGeneration } from '../../shared/browser-tasks/wire.mjs';
import { createAuthenticationTaskSession } from './authentication-task-session.mjs';
export const DESKTOP_PORT_NAME = PORT;
export const DESKTOP_LINK_STORAGE_KEY = 'desktop_link_v1';

export function admitDesktopSender(sender, origin = LOCAL_SERVICE_ORIGIN) {
  return sender?.origin === origin && sender.id === undefined && sender.frameId === 0
    && Number.isInteger(sender.tab?.id) && sender.tab.id >= 0 && Number.isInteger(sender.tab.windowId)
    && typeof sender.documentId === 'string' && sender.documentId.length > 0
    && helperGeneration(sender.url, origin) !== null;
}

export function desktopStage({ consent, legal, pairing }) {
  if (!consent || !legal?.configured) return 'unavailable';
  const mode = consent.consent?.mode;
  const active = mode === 'preview' || mode === 'full';
  const normalPaused = mode === 'paused' && !legal.requires_reauthorization;
  const flow = legal.flow ?? {};
  if (legal.requires_reauthorization
    || (!active && !normalPaused && (!flow.terms_event_id || !flow.risk_event_id || flow.stage === 'pre_mode'))) {
    return 'needs_terms';
  }
  if (mode === 'paused') return 'paused';
  if (mode !== 'full') return 'needs_full';
  if (consent.phase === 'permission_required') return 'needs_site_access';
  if (pairing?.state === 'paired') return 'paired';
  if (pairing?.state === 'pairing' || pairing?.state === 'compare') return 'pairing';
  if (pairing?.state === 'setup_incomplete' || pairing?.state === 'unavailable') return 'needs_account';
  return 'ready_to_pair';
}

// Serialized into the exact admitted document's isolated world. No tab-id-only
// fallback can close a document that replaced the helper while this was queued.
export function closeFinishedHelper(generation, expiresAt) {
  const expected = 'http://bridge.localhost:17871/browser-tasks#presentation=' + generation;
  if (typeof generation !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(generation)
    || window.top !== window || window.location.href !== expected
    || !Number.isFinite(expiresAt) || Date.now() >= expiresAt * 1000) return false;
  window.close();
  return true;
}

export function registerDesktopPort({ chromeApi = globalThis.chrome, owner, companion, authentication,
  onPaired = async () => {}, changeSources = [], now = Date.now } = {}) {
  if (!owner || !companion) throw TypeError('Browser task owners are required');
  const ports = new Set(), tasks = new Map();
  let queue = Promise.resolve(), scheduled = false, registered = false;
  const serial = work => { const result = queue.then(work); queue = result.catch(() => undefined); return result; };
  const writeLink = () => Promise.resolve(chromeApi.storage.session.set({ [DESKTOP_LINK_STORAGE_KEY]: tasks.size })).catch(() => undefined);
  const close = entry => {
    if (entry.closed) return;
    entry.closed = true; clearTimeout(entry.timer); ports.delete(entry);
    if (tasks.get(entry.payload?.task.task_id) === entry) tasks.delete(entry.payload.task.task_id);
    entry.unsubscribe?.(); entry.controller.abort(); companion.cancelFor(entry);
    if (entry.authSession) entry.authSession.close();
    else void owner.release?.(entry.context)?.catch(() => undefined);
    try { entry.port.disconnect(); } catch { /* disconnected */ }
    void writeLink();
  };
  function send(entry, value) { if (!entry.closed) { try { entry.port.postMessage(value); } catch { close(entry); } } }
  function refuse(entry, reason) {
    send(entry, { type: 'refused', reason: ['profile_mismatch', 'account_mismatch', 'task_expired', 'task_changed'].includes(reason) ? reason : 'unavailable' });
    close(entry);
  }
  function assertLive(entry) {
    if (entry.closed || entry.controller.signal.aborted || !entry.payload) throw Error('task_changed');
    if (entry.payload.task.expires_at * 1000 <= now()) throw Error('task_expired');
  }
  async function current(entry) {
    assertLive(entry);
    const tab = await chromeApi.tabs.get(entry.port.sender.tab.id);
    assertLive(entry);
    if (tab.url !== entry.port.sender.url || tab.windowId !== entry.port.sender.tab.windowId) throw Error('task_changed');
    return tab;
  }
  async function push(entry) {
    await current(entry);
    const observation = await owner.read(entry.context);
    await current(entry);
    if (!isObservation(observation)) throw Error('unavailable');
    const encoded = JSON.stringify(observation);
    if (encoded !== entry.last) {
      entry.last = encoded;
      entry.authSession?.invalidate();
      send(entry, { type: 'state', observation });
    }
    if (entry.payload.task.purpose === 'browser.authentication' && ['ready', 'paused'].includes(observation.state)) {
      if (!authentication) throw Error('unavailable');
      await entry.authSession.refresh();
    }
  }
  function changed() {
    if (scheduled || tasks.size === 0) return;
    scheduled = true;
    void serial(async () => {
      scheduled = false;
      for (const entry of tasks.values()) {
        try { await push(entry); } catch (error) { refuse(entry, error.message); }
      }
    });
  }
  function connect(port) {
    if (port?.name !== PORT) return;
    if (!admitDesktopSender(port.sender)) { try { port.disconnect(); } catch { /* disconnected */ } return; }
    const entry = { port, closed: false, desktop: true, replacement: null,
      pairingPending: false, pairingCompleted: false,
      payload: null, context: null, last: null, authSession: null,
      controller: new AbortController(), lastOpen: -Infinity, timer: null, unsubscribe: null };
    ports.add(entry);
    entry.timer = setTimeout(() => refuse(entry, 'unavailable'), 5000);
    port.onDisconnect.addListener(() => close(entry));
    port.onMessage.addListener(message => {
      if (JSON.stringify(message)?.length > 8192) { refuse(entry, 'task_changed'); return; }
      void serial(async () => {
        if (entry.closed) return;
        if (exact(message, ['type', 'payload']) && message.type === 'attach') {
          const payload = message.payload;
          if (entry.payload || !isPayload(payload) || payload.extension_id !== chromeApi.runtime.id
            || payload.task.admission_generation !== helperGeneration(port.sender.url)
            || !['pending', 'accepted', 'outcome_unknown', 'completed'].includes(payload.task.state)) throw Error('task_changed');
          const remaining = payload.task.expires_at * 1000 - now();
          if (remaining <= 0 || remaining > 1800000) throw Error('task_expired');
          const previous = tasks.get(payload.task.task_id);
          if (previous?.payload.task.admission_generation === payload.task.admission_generation) throw Error('task_changed');
          if (previous && ['journey_id', 'operation_id', 'purpose', 'scope_digest', 'source_revision', 'expires_at']
            .some(key => previous.payload.task[key] !== payload.task[key])) throw Error('task_changed');
          entry.payload = payload;
          await current(entry);
          entry.context = await owner.attach(payload, { authorize: () => current(entry), signal: entry.controller.signal });
          try { await current(entry); }
          catch (error) {
            const detached = entry.context; entry.context = null;
            await owner.release?.(detached); throw error;
          }
          if (previous) refuse(previous, 'task_changed');
          tasks.set(payload.task.task_id, entry);
          clearTimeout(entry.timer);
          if (payload.task.purpose === 'browser.authentication') {
            if (!authentication) throw Error('unavailable');
            entry.authSession = createAuthenticationTaskSession({ owner, context: entry.context,
              authentication, payload, authorize: () => current(entry), signal: entry.controller.signal,
              deadline: payload.task.expires_at * 1000, now, changed,
              publish: status => send(entry, { type: 'authentication', task_id: payload.task.task_id,
                admission_generation: payload.task.admission_generation, ...status }),
              onClose: reason => reason === 'expired' ? refuse(entry, 'task_expired') : close(entry) });
          } else entry.timer = setTimeout(() => refuse(entry, 'task_expired'), remaining);
          entry.unsubscribe = companion.subscribe(announced => {
            if (!entry.closed && payload.task.purpose === 'browser.pairing-confirmation' && companion.owns(entry)) {
              if (announced?.state === 'compare' && /^\d{6}$/u.test(announced.comparison_code ?? '')) {
                send(entry, { type: 'pairing', task_id: payload.task.task_id, admission_generation: payload.task.admission_generation,
                  state: 'compare', comparison_code: announced.comparison_code });
              }
            }
          });
          void writeLink(); await push(entry); return;
        }
        if (!exact(message, ['type', 'task_id', 'admission_generation']) || !entry.payload
          || message.task_id !== entry.payload.task.task_id || message.admission_generation !== entry.payload.task.admission_generation) throw Error('task_changed');
        await current(entry);
        const purpose = entry.payload.task.purpose;
        if (message.type === 'finish') {
          if (purpose === 'browser.pairing-confirmation' && !entry.pairingCompleted) throw Error('task_changed');
          const tab = await current(entry);
          if (tab.pendingUrl) throw Error('task_changed');
          const documents = await chromeApi.scripting.executeScript({
            target: { tabId: entry.port.sender.tab.id, documentIds: [entry.port.sender.documentId] },
            world: 'ISOLATED', func: closeFinishedHelper,
            args: [entry.payload.task.admission_generation, entry.payload.task.expires_at],
          });
          if (entry.closed) return;
          assertLive(entry);
          if (documents.length !== 1 || documents[0].frameId !== 0
            || documents[0].documentId !== entry.port.sender.documentId || documents[0].result !== true) throw Error('task_changed');
          send(entry, { type: 'finished', task_id: entry.payload.task.task_id,
            admission_generation: entry.payload.task.admission_generation });
          close(entry); return;
        }
        if (message.type === 'ping') {
          send(entry, { type: 'pong', task_id: entry.payload.task.task_id, admission_generation: entry.payload.task.admission_generation });
          return;
        }
        if (purpose === 'browser.authentication' && message.type === 'authenticate' && authentication) {
          await authentication.launch(entry.payload, async () => {
            await current(entry);
            const observed = await owner.read(entry.context);
            await current(entry);
            if (!['ready', 'paused'].includes(observed.state) || !isObservation(observed)
              || observed.creator_account_id !== entry.payload.creator_account_id) throw Error('task_changed');
          });
          await push(entry); return;
        }
        if (message.type === 'open' && ['browser.access', 'browser.identity', 'browser.history-permission'].includes(purpose)) {
          if (now() - entry.lastOpen < 1000) return;
          entry.lastOpen = now(); await owner.open(entry.context, () => current(entry)); await push(entry); return;
        }
        if (purpose === 'browser.pairing-confirmation' && message.type === 'cancel') { companion.cancelFor(entry); return; }
        if (purpose === 'browser.pairing-confirmation' && ['pair', 'replace-pairing'].includes(message.type)) {
          if (entry.pairingPending || entry.pairingCompleted) return;
          const taskId = entry.payload.task.task_id, admissionGeneration = entry.payload.task.admission_generation;
          const assertCurrent = () => {
            assertLive(entry);
            if (tasks.get(taskId) !== entry || entry.payload.task.task_id !== taskId
              || entry.payload.task.admission_generation !== admissionGeneration) throw Error('task_changed');
          };
          const authorize = async () => {
            const observed = await owner.read(entry.context);
            await current(entry);
            if (!isObservation(observed) || !['ready', 'paused'].includes(observed.state)
              || observed.creator_account_id !== entry.payload.creator_account_id) throw Error('task_changed');
          };
          await authorize();
          let replacement = null;
          if (message.type === 'replace-pairing') {
            if (entry.replacement === null) throw Error('task_changed');
            replacement = entry.replacement;
            entry.replacement = null;
          } else {
            entry.replacement = await companion.savedPairing();
            await authorize();
            if (entry.replacement !== null) {
              send(entry, { type: 'pairing-saved', task_id: entry.payload.task.task_id,
                admission_generation: entry.payload.task.admission_generation });
              return;
            }
          }
          // The independent port code is visible only to this exact task owner.
          entry.pairingPending = true;
          void companion.pairFor(entry, { signal: entry.controller.signal, onPaired, replacement, authorize, assertCurrent })
            .then(async completed => {
              if (completed !== true) return;
              // Brain's comparison result precedes the Agent's authenticated
              // pin commit. Only this completed attempt may release its helper.
              await authorize(); assertCurrent();
              entry.pairingCompleted = true;
              send(entry, { type: 'pairing-completed', task_id: taskId, admission_generation: admissionGeneration });
            })
            .catch(() => refuse(entry, 'unavailable'))
            .finally(() => { entry.pairingPending = false; });
          return;
        }
        throw Error('task_changed');
      }).catch(error => refuse(entry, error.message));
    });
  }
  const unsubscribers = [];
  return Object.freeze({
    register() {
      if (registered) return; registered = true;
      void writeLink(); chromeApi.runtime.onConnectExternal.addListener(connect);
      for (const source of changeSources) unsubscribers.push(source(changed));
    },
    changed,
    get connected() { return tasks.size; },
    unregister() {
      if (!registered) return; registered = false;
      chromeApi.runtime.onConnectExternal.removeListener?.(connect);
      for (const remove of unsubscribers.splice(0)) remove?.();
      for (const entry of ports) close(entry);
    },
  });
}
