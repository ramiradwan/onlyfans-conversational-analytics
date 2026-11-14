import { LOCAL_SIGN_IN_TIMING_WS } from '../transport/local-service-endpoints.mjs';

export function observePopupRemoval(elapsedMs) {
  if (!Number.isFinite(elapsedMs) || elapsedMs < 0 || elapsedMs > 3600000) return;
  try {
    const socket = new WebSocket(LOCAL_SIGN_IN_TIMING_WS);
    const timer = setTimeout(() => socket.close(), 2000);
    socket.onclose = socket.onerror = () => { clearTimeout(timer); };
    socket.onopen = () => {
      socket.send(JSON.stringify({ operation: 'extension', phase: 'popup-remove', status: 'ok', elapsed_ms: Math.round(elapsedMs) }));
    };
  } catch { /* Timing cannot delay task completion. */ }
}
