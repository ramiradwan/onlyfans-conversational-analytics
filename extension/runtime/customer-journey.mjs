import { LOCAL_PAIRING_WS } from '../transport/local-service-endpoints.mjs';

export const CUSTOMER_STATES = Object.freeze({
  PREVIEW_AVAILABLE: 'preview_available',
  PAUSED: 'paused',
  DESKTOP_APP_NEEDED: 'desktop_app_needed',
  DESKTOP_APP_UNAVAILABLE: 'desktop_app_unavailable',
  SETUP_INCOMPLETE: 'setup_incomplete',
  PAIRING_REQUIRED: 'pairing_required',
  PAIRING_IN_PROGRESS: 'pairing_in_progress',
  PAIRING_FAILED: 'pairing_failed',
  PAIRING_NOT_READY: 'pairing_not_ready',
  ACTIVATION_CHECKING: 'activation_checking',
  ACTIVATION_REQUIRED: 'activation_required',
  ACTIVATION_ACTIVE: 'activation_active',
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
  resumeAvailable = false,
} = {}) {
  if (status?.consent?.mode === 'paused') {
    return Object.freeze({
      id: CUSTOMER_STATES.PAUSED,
      tone: 'info',
      title: 'Analytics paused',
      body: resumeAvailable
        ? 'No new activity is collected.'
        : 'No new activity is collected. Review the updated information to resume.',
      primaryAction: resumeAvailable ? 'resume' : null,
      primaryLabel: resumeAvailable ? 'Resume analytics' : null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (!fullConsent(status)) {
    const preview = status?.consent?.mode === 'preview';
    return Object.freeze({
      id: CUSTOMER_STATES.PREVIEW_AVAILABLE,
      tone: 'info',
      title: preview ? 'Preview is ready' : 'Start with Preview',
      body: preview
        ? 'Add Full analysis for insights from your conversations.'
        : 'Count activity in this browser without keeping message text.',
      primaryAction: preview ? 'review_full' : null,
      primaryLabel: preview ? 'Add Full analysis' : null,
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
        : 'Keep this window open.',
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
      body: 'In the desktop app, choose Connect extension and try again.',
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
        body: 'Start the desktop app, then try again. Your connection is saved.',
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
        ? 'Full analysis runs on this computer. Preview works without the desktop app.'
        : 'The download is unavailable. You can still use Preview.',
      primaryAction: desktopDownloadAvailable ? 'install_desktop' : null,
      primaryLabel: desktopDownloadAvailable ? 'Install desktop app' : null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (pairing?.state === 'desktop_not_ready') {
    return Object.freeze({
      id: CUSTOMER_STATES.PAIRING_NOT_READY,
      tone: 'warning',
      title: 'Continue in the desktop app',
      body: 'In the desktop app, open Settings and choose Connect extension.',
      primaryAction: 'open_desktop_settings',
      primaryLabel: 'Open desktop app settings',
      secondaryAction: 'pair',
      secondaryLabel: 'Pair device',
    });
  }

  if (pairing?.state === 'setup_incomplete' || pairing?.state === 'unavailable') {
    return Object.freeze({
      id: CUSTOMER_STATES.SETUP_INCOMPLETE,
      tone: 'warning',
      title: 'Sign in to your creator account',
      body: 'Use OnlyFans in this browser, then return here.',
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
      title: 'Connect to the desktop app',
      body: 'Connect to view insights from your conversations.',
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
      body: '',
      primaryAction: 'retry_full',
      primaryLabel: 'Retry connection',
      secondaryAction: 'open_dashboard',
      secondaryLabel: 'Open desktop app',
    });
  }

  if (analysisReadiness.commercial_authority === 'unknown') {
    return Object.freeze({
      id: CUSTOMER_STATES.ACTIVATION_CHECKING,
      tone: 'progress',
      title: 'Checking activation',
      body: '',
      primaryAction: null,
      primaryLabel: null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (analysisReadiness.commercial_authority === 'required') {
    return Object.freeze({
      id: CUSTOMER_STATES.ACTIVATION_REQUIRED,
      tone: 'warning',
      title: 'Finish activating Full analysis',
      body: 'Continue in Settings in the desktop app.',
      primaryAction: 'open_dashboard',
      primaryLabel: 'Open desktop app',
      secondaryAction: 'retry_readiness',
      secondaryLabel: 'Check activation',
    });
  }

  if (analysisReadiness.commercial_authority === 'unavailable') {
    return Object.freeze({
      id: CUSTOMER_STATES.ACTIVATION_UNAVAILABLE,
      tone: 'error',
      title: 'Couldn\'t check activation',
      body: 'Your saved data is unchanged.',
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
      title: 'Your analysis is ready',
      body: '',
      primaryAction: 'open_dashboard',
      primaryLabel: 'Open analysis',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (
    analysisReadiness.commercial_authority === 'active'
    && analysisReadiness.analysis_admission === 'blocked'
  ) {
    return Object.freeze({
      id: CUSTOMER_STATES.ACTIVATION_ACTIVE,
      tone: 'warning',
      title: 'Analysis is not available right now',
      body: 'Full analysis is activated. Your saved data is unchanged.',
      primaryAction: 'retry_readiness',
      primaryLabel: 'Check again',
      secondaryAction: 'open_dashboard',
      secondaryLabel: 'Open desktop app',
    });
  }

  return Object.freeze({
    id: CUSTOMER_STATES.FULL_UNAVAILABLE,
    tone: 'warning',
    title: 'Full analysis is temporarily unavailable',
    body: 'Open the desktop app to see what needs attention.',
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
