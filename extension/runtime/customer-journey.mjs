import { LOCAL_PAIRING_WS } from '../transport/local-service-endpoints.mjs';

export const CUSTOMER_STATES = Object.freeze({
  PREVIEW_AVAILABLE: 'preview_available',
  DESKTOP_APP_NEEDED: 'desktop_app_needed',
  DESKTOP_APP_UNAVAILABLE: 'desktop_app_unavailable',
  PAIRING_REQUIRED: 'pairing_required',
  PAIRING_IN_PROGRESS: 'pairing_in_progress',
  PAIRING_FAILED: 'pairing_failed',
  FULL_READY: 'full_ready',
  FULL_UNAVAILABLE: 'full_unavailable',
});

function fullConsent(status) {
  return status?.consent?.mode === 'full';
}

function paired(pairing) {
  return pairing?.state === 'paired';
}

export function deriveCustomerJourney({
  status,
  pairing,
  desktopRuntimeReachable,
  desktopDownloadAvailable = false,
} = {}) {
  if (!fullConsent(status)) {
    return Object.freeze({
      id: CUSTOMER_STATES.PREVIEW_AVAILABLE,
      tone: 'info',
      title: status?.consent?.mode === 'preview' ? 'Preview is ready' : 'Preview is available',
      body: status?.consent?.mode === 'preview'
        ? 'You can keep using Preview without the desktop app. Activate Full analysis when you want message-level insights.'
        : 'Start with a limited seven-day activity view. The desktop app is only required for Full analysis.',
      primaryAction: status?.consent?.mode === 'preview' ? 'review_full' : null,
      primaryLabel: status?.consent?.mode === 'preview' ? 'Activate Full analysis' : null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (pairing?.state === 'pairing' || pairing?.state === 'compare') {
    return Object.freeze({
      id: CUSTOMER_STATES.PAIRING_IN_PROGRESS,
      tone: 'progress',
      title: pairing.state === 'compare' ? 'Confirm the connection' : 'Connecting to the desktop app',
      body: pairing.state === 'compare'
        ? 'Compare the six-digit code here with the code in the desktop app. Confirm only when both codes match.'
        : 'Keep this window open while the extension and desktop app create a secure connection.',
      primaryAction: null,
      primaryLabel: null,
      secondaryAction: 'cancel_pairing',
      secondaryLabel: 'Cancel',
    });
  }

  if (pairing?.state === 'pairing_failed') {
    return Object.freeze({
      id: CUSTOMER_STATES.PAIRING_FAILED,
      tone: 'error',
      title: 'Connection was not completed',
      body: 'Open a new connection window in the desktop app, then try again. No Full data is sent until the connection succeeds.',
      primaryAction: 'pair',
      primaryLabel: 'Try connection again',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (!desktopRuntimeReachable) {
    if (paired(pairing)) {
      return Object.freeze({
        id: CUSTOMER_STATES.DESKTOP_APP_UNAVAILABLE,
        tone: 'warning',
        title: 'Desktop app is not available',
        body: 'Your previous connection is saved. Start the desktop app, then retry. Preview remains available while Full analysis is offline.',
        primaryAction: 'retry_full',
        primaryLabel: 'Retry connection',
        secondaryAction: null,
        secondaryLabel: null,
      });
    }
    return Object.freeze({
      id: CUSTOMER_STATES.DESKTOP_APP_NEEDED,
      tone: 'warning',
      title: 'Desktop app needed for Full analysis',
      body: desktopDownloadAvailable
        ? 'Full analysis runs through the desktop app on this computer. Install it first; Preview can still be used without it.'
        : 'Full analysis runs through the desktop app on this computer. This build does not yet include the customer download link; Preview still works independently.',
      primaryAction: desktopDownloadAvailable ? 'install_desktop' : null,
      primaryLabel: desktopDownloadAvailable ? 'Install desktop app' : null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (!paired(pairing)) {
    return Object.freeze({
      id: CUSTOMER_STATES.PAIRING_REQUIRED,
      tone: 'info',
      title: 'Connect this extension to the desktop app',
      body: 'The desktop app is running. Connect the two so Full analysis can use the approved local setup.',
      primaryAction: 'pair',
      primaryLabel: 'Pair device',
      secondaryAction: 'open_dashboard',
      secondaryLabel: 'Open desktop app',
    });
  }

  const fullReady = status?.phase === 'full'
    && status?.delivery?.transport_state === 'authenticated';
  if (fullReady) {
    return Object.freeze({
      id: CUSTOMER_STATES.FULL_READY,
      tone: 'success',
      title: 'Full mode is ready',
      body: 'The extension is connected to the desktop app and Full analysis can receive new activity.',
      primaryAction: 'open_dashboard',
      primaryLabel: 'Open analysis',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  return Object.freeze({
    id: CUSTOMER_STATES.FULL_UNAVAILABLE,
    tone: 'warning',
    title: 'Full mode is temporarily unavailable',
    body: 'The saved connection is present, but Full analysis is not ready right now. Retry after the desktop app is fully started.',
    primaryAction: 'retry_full',
    primaryLabel: 'Retry connection',
    secondaryAction: 'open_dashboard',
    secondaryLabel: 'Open desktop app',
  });
}

export async function probeDesktopRuntime({
  url = LOCAL_PAIRING_WS,
  webSocketFactory = (value) => new WebSocket(value),
  timeoutMs = 900,
} = {}) {
  let socket;
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { socket?.close(1000, 'readiness_probe_complete'); } catch {}
      resolve(value);
    };
    const timer = setTimeout(() => finish(false), timeoutMs);
    try {
      socket = webSocketFactory(url);
      socket.onopen = () => finish(true);
      socket.onerror = () => finish(false);
      socket.onclose = () => finish(false);
    } catch {
      finish(false);
    }
  });
}
