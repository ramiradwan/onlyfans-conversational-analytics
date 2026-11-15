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

/** From the helper's open request to the popup holding the ceremony page. Navigation starts there. */
export function observePopupCreation(elapsedMs) { observe('popup-create', elapsedMs); }
