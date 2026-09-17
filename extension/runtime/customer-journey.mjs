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
      title: 'Analytics are paused',
      body: resumeAvailable
        ? 'Nothing new is collected while paused. Resume whenever you are ready.'
        : 'Nothing new is collected while paused. Review the updated information below to continue.',
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
        ? 'Your activity counts update as you use OnlyFans. For insights from your conversations, add Full analysis.'
        : 'Preview counts messages and chats in this browser for seven days, without keeping message text. You can add Full analysis later.',
      primaryAction: preview ? 'review_full' : null,
      primaryLabel: preview ? 'Activate Full analysis' : null,
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
      body: 'In the desktop app, choose Connect extension again. Then try again here. Nothing is shared until the connection works.',
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
        ? 'Full analysis runs in the desktop app on this computer. Preview can still be used without it.'
        : 'The desktop app download is not available yet. You can keep using Preview in the meantime.',
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
      body: 'Open Settings in the desktop app and choose Connect extension. Then choose Pair device here.',
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
      body: 'The desktop app is running. Pair it with this extension to start Full analysis.',
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
      body: 'Paired. The extension is finishing its secure connection to the desktop app.',
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
      body: 'Checking whether Full analysis is active in the desktop app.',
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
      title: 'Full activation required',
      body: 'To see insights from your conversations, finish activation in Settings in the desktop app.',
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
      title: 'Full activation needs attention',
      body: 'Activation could not be confirmed right now. Check again in a moment; your saved data is not affected.',
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
      title: 'Full analysis is ready',
      body: 'Everything is connected. Your insights are in the desktop app.',
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
      body: 'Full activation is active, but analysis cannot run at the moment. Check again shortly; your saved data is not affected.',
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
    body: 'Check again, or open the desktop app to see what needs attention.',
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
