import { LOCAL_SIGN_IN_TIMING_WS } from '../transport/local-service-endpoints.mjs';

function observe(phase, elapsedMs) {
  if (!Number.isFinite(elapsedMs) || elapsedMs < 0 || elapsedMs > 3600000) return;
  try {
    const socket = new WebSocket(LOCAL_SIGN_IN_TIMING_WS);
    const timer = setTimeout(() => socket.close(), 2000);
    socket.onclose = socket.onerror = () => { clearTimeout(timer); };
    socket.onopen = () => {
      socket.send(JSON.stringify({ operation: 'extension', phase, status: 'ok', elapsed_ms: Math.round(elapsedMs) }));
    };
  } catch { /* Timing cannot delay a sign-in. */ }
}

export function observePopupRemoval(elapsedMs) { observe('popup-remove', elapsedMs); }

const POPUP_LIFECYCLE = new Set(['popup-close-observed', 'popup-end-requested', 'popup-saved-end-requested',
  'popup-expiry-requested', 'popup-waiting-end-requested', 'popup-waiting-restart-requested', 'popup-navigation-failed',
  'popup-helper-end-received', 'popup-paired-end-received']);

export function observePopupLifecycle(phase) { if (POPUP_LIFECYCLE.has(phase)) observe(phase, 0); }

/** From the helper's open request to the popup holding the ceremony page. Navigation starts there. */
export function observePopupCreation(elapsedMs) { observe('popup-create', elapsedMs); }

// Stage marks of one sign-in attach, held in memory and sent together on one socket.
const stages = new Map();
const STAGE_LIMIT = 16;

/** Starts a task's stage timeline. `elapsedMs` is the time since its read ahead began, or 0. */
export function stageBegin(taskId, elapsedMs = 0, clock = () => performance.now()) {
  if (stages.size >= 8 && !stages.has(taskId)) stages.delete(stages.keys().next().value);
  stages.set(taskId, { started: clock(), clock, marks: [] });
  stageMark(taskId, 'attach-received', elapsedMs);
}

/** Records one stage, timed from the attach unless an elapsed time is given. */
export function stageMark(taskId, phase, elapsedMs) {
  const stage = stages.get(taskId);
  if (!stage || stage.marks.length >= STAGE_LIMIT) return;
  const elapsed = elapsedMs ?? stage.clock() - stage.started;
  if (!Number.isFinite(elapsed) || elapsed < 0 || elapsed > 3600000) return;
  stage.marks.push({ operation: 'extension', phase, status: 'ok', elapsed_ms: Math.round(elapsed) });
}

/** Sends a task's recorded stages and forgets them. */
export function stageFlush(taskId, open = url => new WebSocket(url)) {
  const stage = stages.get(taskId);
  stages.delete(taskId);
  if (!stage?.marks.length) return;
  try {
    const socket = open(LOCAL_SIGN_IN_TIMING_WS);
    const timer = setTimeout(() => socket.close(), 2000);
    socket.onclose = socket.onerror = () => { clearTimeout(timer); };
    socket.onopen = () => { socket.send(JSON.stringify({ marks: stage.marks })); };
  } catch { /* Timing cannot delay a sign-in. */ }
}

/** A popup owner's stage. The window navigation is the last one recorded, so the task's stages are sent there. */
export function observeSignInStage(taskId, phase) {
  stageMark(taskId, phase);
  if (phase === 'navigate-sent') stageFlush(taskId);
}
