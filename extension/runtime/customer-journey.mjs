import { LOCAL_PAIRING_WS } from '../transport/local-service-endpoints.mjs';

export const CUSTOMER_STATES = Object.freeze({
  PREVIEW_AVAILABLE: 'preview_available',
  DESKTOP_APP_NEEDED: 'desktop_app_needed',
  DESKTOP_APP_UNAVAILABLE: 'desktop_app_unavailable',
  SETUP_INCOMPLETE: 'setup_incomplete',
  PAIRING_REQUIRED: 'pairing_required',
  PAIRING_IN_PROGRESS: 'pairing_in_progress',
  PAIRING_FAILED: 'pairing_failed',
  ACTIVATION_REQUIRED: 'activation_required',
  ACTIVATION_UNAVAILABLE: 'activation_unavailable',
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
  analysisReadiness = { commercial_authority: 'unknown', analysis_admission: 'blocked' },
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
        title: 'Desktop app is not running',
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
        : 'The desktop app download is not available from this release yet. You can keep using Preview in the meantime.',
      primaryAction: desktopDownloadAvailable ? 'install_desktop' : null,
      primaryLabel: desktopDownloadAvailable ? 'Install desktop app' : null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (pairing?.state === 'setup_incomplete' || pairing?.state === 'unavailable') {
    return Object.freeze({
      id: CUSTOMER_STATES.SETUP_INCOMPLETE,
      tone: 'warning',
      title: 'Open your creator account to continue',
      body: 'Open OnlyFans and sign in to the creator account you want to analyze. Then return here to connect the extension.',
      primaryAction: 'open_creator_account',
      primaryLabel: 'Open creator account',
      secondaryAction: 'open_dashboard',
      secondaryLabel: 'Open desktop app',
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

  if (status?.delivery?.transport_state !== 'authenticated') {
    return Object.freeze({
      id: CUSTOMER_STATES.FULL_UNAVAILABLE,
      tone: 'progress',
      title: 'Finishing the desktop connection',
      body: 'The devices are paired. Full analysis is waiting for the authenticated local delivery connection to finish.',
      primaryAction: 'retry_full',
      primaryLabel: 'Retry connection',
      secondaryAction: 'open_dashboard',
      secondaryLabel: 'Open desktop app',
    });
  }

  if (analysisReadiness.commercial_authority === 'required') {
    return Object.freeze({
      id: CUSTOMER_STATES.ACTIVATION_REQUIRED,
      tone: 'warning',
      title: 'Activate Full analysis',
      body: 'The desktop connection is ready, but paid Full analysis is not activated yet. Open the desktop app to continue account setup.',
      primaryAction: 'open_dashboard',
      primaryLabel: 'Open desktop app',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (analysisReadiness.commercial_authority === 'unavailable') {
    return Object.freeze({
      id: CUSTOMER_STATES.ACTIVATION_UNAVAILABLE,
      tone: 'error',
      title: 'Full activation needs attention',
      body: 'The desktop connection is ready, but Full analysis cannot confirm current commercial authorization. Retry or open the desktop app for recovery.',
      primaryAction: 'retry_readiness',
      primaryLabel: 'Check again',
      secondaryAction: 'open_dashboard',
      secondaryLabel: 'Open desktop app',
    });
  }

  if (
    analysisReadiness.commercial_authority === 'active'
    && analysisReadiness.analysis_admission === 'admitted'
  ) {
    return Object.freeze({
      id: CUSTOMER_STATES.FULL_READY,
      tone: 'success',
      title: 'Full mode is ready',
      body: 'The desktop app is securely connected, Full activation is active, and licensed analysis is ready.',
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
    body: 'The desktop connection and activation are present, but licensed analysis is not admitted right now. Check again or open the desktop app.',
    primaryAction: 'retry_readiness',
    primaryLabel: 'Check again',
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
