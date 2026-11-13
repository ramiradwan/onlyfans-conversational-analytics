import { PORT, exact, isObservation, isAuthentication, isPairingComparison, isSavedPairing, isPairingCompleted, sameTask } from '../../shared/browser-tasks/wire.mjs';

/** Only the real Chrome helper imports runtime.connect. Discovery is not absence. */
export const FOCUS_TIMEOUT_MS = 2000;
export function connectExtension({ payload, receive, runtime = globalThis.chrome?.runtime, timeout = 5000 }) {
  let port, timer, heartbeat, closed = false, last = null, finishing = false, finishResolve = null;
  let focusResolve = null, focusTimer = null, refused = false;
  const own = value => exact(value, ['type', 'task_id', 'admission_generation'])
    && value.task_id === payload.task.task_id && value.admission_generation === payload.task.admission_generation;
  const close = () => {
    if (closed) return;
    closed = true; clearTimeout(timer); clearInterval(heartbeat);
    finishResolve?.(false); finishResolve = null;
    clearTimeout(focusTimer); focusResolve?.(false); focusResolve = null;
    try { port?.disconnect(); } catch { /* disconnected */ }
  };
  const unavailable = () => { if (closed) return; close(); if (!finishing) receive({ state: 'unavailable' }); };
  timer = setTimeout(unavailable, timeout);
  try {
    if (!runtime?.connect) throw new Error('unavailable');
    port = runtime.connect(payload.extension_id, { name: PORT });
    port.onDisconnect.addListener(() => { void runtime.lastError; refused = false; unavailable(); });
    port.onMessage.addListener(value => {
      if (closed) return;
      if (finishing) {
        if (!['finished', 'refused'].includes(value?.type)) return;
        const exactFinish = exact(value, ['type', 'task_id', 'admission_generation']) && value.type === 'finished'
          && value.task_id === payload.task.task_id && value.admission_generation === payload.task.admission_generation;
        finishResolve?.(exactFinish); finishResolve = null; close(); return;
      }
      if (exact(value, ['type', 'reason']) && value.type === 'refused'
        && ['profile_mismatch', 'account_mismatch', 'task_expired', 'task_changed', 'unavailable'].includes(value.reason)) {
        // The extension keeps a refused port briefly so this helper can ask to be closed.
        const kept = port;
        refused = true; closed = true; clearTimeout(timer); clearInterval(heartbeat);
        clearTimeout(focusTimer); focusResolve?.(false); focusResolve = null;
        timer = setTimeout(() => { refused = false; try { kept.disconnect(); } catch { /* disconnected */ } }, timeout);
        receive({ state: value.reason }); return;
      }
      if (last && focusResolve && own(value) && value.type === 'focused') {
        clearTimeout(focusTimer); focusResolve(true); focusResolve = null; return;
      }
      if (last && exact(value, ['type', 'task_id', 'admission_generation']) && value.type === 'pong'
        && value.task_id === payload.task.task_id && value.admission_generation === payload.task.admission_generation) {
        clearTimeout(timer); return;
      }
      if (last && isAuthentication(value, payload)) {
        clearTimeout(timer); receive({ state: 'auth_' + value.state, notification: value.notification }); return;
      }
      if (last && isPairingComparison(value, payload)) {
        clearTimeout(timer); receive({ state: 'pairing_compare', comparison_code: value.comparison_code }); return;
      }
      if (last && isSavedPairing(value, payload)) {
        clearTimeout(timer); receive({ state: 'pairing_saved' }); return;
      }
      if (last && isPairingCompleted(value, payload)) {
        clearTimeout(timer); receive({ state: 'pairing_completed' }); return;
      }
      if (!exact(value, ['type', 'observation']) || value.type !== 'state' || !isObservation(value.observation)
        || !sameTask(value.observation, payload.task) || value.observation.extension_id !== payload.extension_id) { unavailable(); return; }
      const observation = value.observation;
      if (last && (last.instance_id !== observation.instance_id || last.epoch !== observation.epoch
        || observation.revision < last.revision || (observation.revision === last.revision && JSON.stringify(last) !== JSON.stringify(observation)))) {
        unavailable(); return;
      }
      clearTimeout(timer); last = observation;
      // An idle MV3 worker closes its ports. Keep only this finite admitted task
      // live; these acknowledgements neither read account data nor renew expiry.
      heartbeat ??= setInterval(() => {
        timer = setTimeout(unavailable, timeout);
        try { port.postMessage({ type: 'ping', task_id: payload.task.task_id, admission_generation: payload.task.admission_generation }); }
        catch { unavailable(); }
      }, 20000);
      receive({ state: observation.state, observation });
    });
    port.postMessage({ type: 'attach', payload });
  } catch { queueMicrotask(unavailable); }
  function send(type) {
    if (closed || !last) return false;
    try { port.postMessage({ type, task_id: payload.task.task_id, admission_generation: payload.task.admission_generation }); return true; }
    catch { unavailable(); return false; }
  }
  return { close() { if (!refused) close(); }, finish() {
    if (closed || finishing || !last) return Promise.resolve(false);
    finishing = true; clearTimeout(timer); clearInterval(heartbeat);
    return new Promise(resolve => {
      finishResolve = resolve;
      timer = setTimeout(close, timeout);
      if (!send('finish')) close();
    });
  }, focus() {
    if (closed || !last || focusResolve) return Promise.resolve(false);
    return new Promise(resolve => {
      focusResolve = resolve;
      focusTimer = setTimeout(() => { focusResolve = null; resolve(false); }, FOCUS_TIMEOUT_MS);
      if (!send('focus')) { clearTimeout(focusTimer); focusResolve = null; resolve(false); }
    });
  }, end() {
    // Asks the extension to close this exact helper document. Only after a refusal or while attached.
    if (!refused && (closed || !last)) return false;
    try {
      port.postMessage({ type: 'close', task_id: payload.task.task_id, admission_generation: payload.task.admission_generation });
    } catch { return false; }
    refused = false; closed = true; clearInterval(heartbeat);
    return true;
  }, open: () => send('open'), authenticate() {
    return payload.task.purpose === 'browser.authentication' && ['ready', 'paused'].includes(last?.state) && send('authenticate');
  }, pair() {
    return payload.task.purpose === 'browser.pairing-confirmation' && ['ready', 'paused'].includes(last?.state) && send('pair');
  }, replacePairing() {
    return payload.task.purpose === 'browser.pairing-confirmation' && ['ready', 'paused'].includes(last?.state) && send('replace-pairing');
  } };
}

/** One fresh port after the first one died: attach, then a single finish or close. */
export function reattachExtension({ payload, type, runtime = globalThis.chrome?.runtime, timeout = 5000 }) {
  if (!['finish', 'close'].includes(type)) return Promise.resolve(false);
  return new Promise(resolve => {
    let port, done = false;
    const end = result => {
      if (done) return;
      done = true; clearTimeout(timer);
      try { port?.disconnect(); } catch { /* disconnected */ }
      resolve(result);
    };
    const timer = setTimeout(() => end(false), timeout);
    try {
      port = runtime.connect(payload.extension_id, { name: PORT });
      port.onDisconnect.addListener(() => { void runtime.lastError; end(false); });
      port.onMessage.addListener(value => {
        if (type === 'finish' && exact(value, ['type', 'task_id', 'admission_generation']) && value.type === 'finished'
          && value.task_id === payload.task.task_id && value.admission_generation === payload.task.admission_generation) end(true);
      });
      port.postMessage({ type: 'attach', payload });
      port.postMessage({ type, task_id: payload.task.task_id, admission_generation: payload.task.admission_generation });
    } catch { end(false); }
  });
}
