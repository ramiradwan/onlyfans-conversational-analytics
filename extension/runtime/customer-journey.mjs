import { LOCAL_PAIRING_WS } from '../transport/local-service-endpoints.mjs';

export const CUSTOMER_STATES = Object.freeze({
  ANALYTICS_OFF: 'analytics_off',
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
  connectionRecovery = 'idle',
  analysisReadiness = { commercial_authority: 'unknown', analysis_admission: 'blocked' },
  resumeAvailable = false,
  modeChoiceAvailable = false,
} = {}, { setup = false } = {}) {
  if (status?.consent?.mode === 'paused') {
    const reviewAvailable = !resumeAvailable && modeChoiceAvailable;
    return Object.freeze({
      id: CUSTOMER_STATES.PAUSED,
      tone: 'info',
      title: 'Analytics paused',
      body: resumeAvailable
        ? 'No new activity is collected.'
        : 'No new activity is collected. Review the updated information to resume.',
      primaryAction: resumeAvailable ? 'resume' : reviewAvailable ? 'choose_mode' : null,
      primaryLabel: resumeAvailable ? 'Resume analytics' : reviewAvailable ? 'Review changes' : null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (status?.phase === 'revoked' || status?.consent?.mode === 'revoked') {
    return Object.freeze({
      id: CUSTOMER_STATES.ANALYTICS_OFF,
      tone: 'info',
      title: 'Analytics off',
      body: 'Site access was removed.',
      primaryAction: modeChoiceAvailable ? 'choose_mode' : null,
      primaryLabel: modeChoiceAvailable ? 'Choose analytics mode' : null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (status?.phase === 'permission_required') {
    return Object.freeze({
      id: CUSTOMER_STATES.SETUP_INCOMPLETE,
      tone: 'warning',
      title: 'Site access needed',
      body: 'Allow site access to receive activity from your creator account.',
      primaryAction: ['preview', 'full'].includes(status.consent.mode) ? 'restore_access' : null,
      primaryLabel: ['preview', 'full'].includes(status.consent.mode) ? 'Allow site access' : null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (status?.consent?.mode === 'preview') {
    return Object.freeze({
      id: CUSTOMER_STATES.PREVIEW_AVAILABLE,
      tone: 'info',
      title: 'Preview is ready',
      body: 'Add Full analytics for insights from your conversations.',
      primaryAction: 'review_full',
      primaryLabel: 'Review Full analytics',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (!fullConsent(status)) {
    return Object.freeze({
      id: CUSTOMER_STATES.ANALYTICS_OFF,
      tone: 'info',
      title: setup ? 'Choose analytics mode' : 'Start with Preview',
      body: 'See how many messages you send and receive each day.',
      primaryAction: modeChoiceAvailable ? 'choose_mode' : null,
      primaryLabel: modeChoiceAvailable ? 'Set up Preview' : null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (pairing?.state === 'pairing' || pairing?.state === 'compare') {
    const comparing = pairing.state === 'compare' && (!setup || pairing.owns_attempt && pairing.comparison_code);
    return Object.freeze({
      id: CUSTOMER_STATES.PAIRING_IN_PROGRESS,
      tone: 'progress',
      title: comparing ? 'Confirm the connection' : 'Connecting to the desktop app',
      body: comparing
        ? setup ? 'Check that the desktop app shows the same code, then confirm there. Keep this tab open.'
          : 'Check that the desktop app shows the same code, then confirm there.'
        : 'Keep this page open.',
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
      title: setup ? 'Connection didn’t finish' : 'Connection was not completed',
      body: setup ? 'Choose Connect extension in the desktop app to start a new connection.' : 'In the desktop app, choose Connect extension.',
      primaryAction: setup ? 'open_desktop' : 'pair',
      primaryLabel: setup ? 'Open desktop app' : 'Try connection again',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (paired(pairing) && !desktopRuntimeReachable) {
    const trying = ['authenticating', 'connecting'].includes(status?.delivery?.transport_state) || connectionRecovery === 'retrying';
    return Object.freeze({
      id: CUSTOMER_STATES.DESKTOP_APP_UNAVAILABLE,
      tone: trying ? 'progress' : 'warning',
      title: trying ? 'Connecting to the desktop app' : 'Desktop connection not confirmed',
      body: trying ? '' : setup ? 'Open the desktop app to finish connecting.' : 'The desktop connection could not be confirmed.',
      primaryAction: trying ? null : 'open_desktop',
      primaryLabel: trying ? null : 'Open desktop app',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (pairing?.state === 'unavailable') {
    const trying = connectionRecovery === 'retrying';
    return Object.freeze({
      id: CUSTOMER_STATES.SETUP_INCOMPLETE,
      tone: trying ? 'progress' : 'warning',
      title: trying ? 'Reconnecting to the extension' : 'Connection status unavailable',
      body: trying ? '' : setup ? 'Dipsy can’t check the connection right now.' : 'The extension connection could not be checked.',
      primaryAction: null,
      primaryLabel: null,
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (!desktopRuntimeReachable) {
    return Object.freeze({
      id: CUSTOMER_STATES.DESKTOP_APP_NEEDED,
      tone: 'warning',
      title: 'Set up the desktop app for Full analytics',
      body: 'Open the desktop app to continue. Install it if you haven’t already.',
      primaryAction: 'open_desktop',
      primaryLabel: 'Open desktop app',
      secondaryAction: desktopDownloadAvailable ? 'install_desktop' : null,
      secondaryLabel: desktopDownloadAvailable ? 'Install desktop app' : null,
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
      secondaryLabel: 'Connect extension',
    });
  }

  if (pairing?.state === 'setup_incomplete') {
    return Object.freeze({
      id: CUSTOMER_STATES.SETUP_INCOMPLETE,
      tone: 'warning',
      title: 'Sign in to your creator account',
      body: 'Use the creator account you want to connect.',
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
      primaryLabel: 'Connect extension',
      secondaryAction: 'open_dashboard',
      secondaryLabel: 'Open desktop app',
    });
  }

  if (status?.delivery?.transport_state !== 'authenticated') {
    const trying = ['authenticating', 'connecting'].includes(status?.delivery?.transport_state) || connectionRecovery === 'retrying';
    return Object.freeze({
      id: CUSTOMER_STATES.FULL_UNAVAILABLE,
      tone: trying ? 'progress' : 'warning',
      title: trying ? 'Connecting to the desktop app' : 'Desktop connection not confirmed',
      body: trying ? '' : setup ? 'Open the desktop app to finish connecting.' : 'The connection could not be confirmed.',
      primaryAction: trying ? null : 'open_dashboard',
      primaryLabel: trying ? null : 'Open desktop app',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (analysisReadiness.commercial_authority === 'unknown') {
    return Object.freeze({
      id: CUSTOMER_STATES.ACTIVATION_CHECKING,
      tone: 'progress',
      title: setup ? 'Checking Full analytics…' : 'Checking activation',
      body: '',
      primaryAction: 'open_dashboard',
      primaryLabel: 'Open desktop app',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (analysisReadiness.commercial_authority === 'required') {
    return Object.freeze({
      id: CUSTOMER_STATES.ACTIVATION_REQUIRED,
      tone: 'warning',
      title: 'Set up Full analytics',
      body: setup ? 'Continue in the desktop app.' : 'Continue in Settings in the desktop app.',
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
      title: setup ? 'Can’t check Full analytics right now' : 'Couldn\'t check activation',
      body: '',
      primaryAction: 'open_dashboard',
      primaryLabel: 'Open desktop app',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (
    analysisReadiness.commercial_authority === 'active'
    && status?.delivery?.browser_tab_sleeping === true
  ) {
    return Object.freeze({
      id: CUSTOMER_STATES.FULL_UNAVAILABLE,
      tone: 'warning',
      title: setup ? 'Updates paused' : 'Open the creator site to continue',
      body: 'Your browser paused the creator site tab.',
      primaryAction: 'open_creator_account',
      primaryLabel: 'Open creator account',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  if (
    analysisReadiness.commercial_authority === 'active'
    && analysisReadiness.analysis_admission === 'admitted'
  ) {
    return Object.freeze({
      id: CUSTOMER_STATES.FULL_READY,
      tone: 'success',
      title: setup ? 'Full analytics is on' : 'Your analysis is ready',
      body: setup ? 'Insights appear in the desktop app.' : '',
      primaryAction: 'open_dashboard',
      primaryLabel: setup ? 'Open desktop app' : 'Open analysis',
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
      title: setup ? 'New messages aren’t being analyzed' : "New messages aren't being analyzed",
      body: '',
      primaryAction: 'open_dashboard',
      primaryLabel: 'Open desktop app',
      secondaryAction: null,
      secondaryLabel: null,
    });
  }

  return Object.freeze({
    id: CUSTOMER_STATES.FULL_UNAVAILABLE,
    tone: 'warning',
    title: setup ? 'Can’t check analysis right now' : 'Analysis status unavailable',
    body: '',
    primaryAction: 'open_dashboard',
    primaryLabel: 'Open desktop app',
    secondaryAction: null,
    secondaryLabel: null,
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
