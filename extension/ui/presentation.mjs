import { deriveCustomerJourney } from '../runtime/customer-journey.mjs';
import { secureExternalUrl } from './surface-client.mjs';

export function phaseLabel(status) {
  return ({ off: 'Analytics off', preview: 'Preview on', identity: 'Full setup in progress',
    full: status.delivery?.transport_state === 'authenticated' ? 'Desktop connected' : 'Full setup in progress',
    paused: 'Analytics paused', revoked: 'Site access revoked', permission_required: 'Site access needs approval',
    transitioning: 'Applying your choice…', unavailable: 'Analytics temporarily unavailable' })[status.phase] ?? 'Analytics inactive';
}
export function modeChoiceAvailable(model) {
  const { legal, status } = model;
  if (!legal?.configured || legal.flow.stage !== 'mode_selection') return false;
  const mode = status?.consent.mode ?? legal.consent_mode;
  return !['preview', 'full'].includes(mode) && !(mode === 'paused' && !legal.requires_reauthorization);
}
export function needsAgreement(model) {
  const { legal, status } = model;
  if (!legal?.configured) return false;
  const active = ['preview', 'full'].includes(status?.consent.mode);
  const normalPaused = status?.consent.mode === 'paused' && !legal.requires_reauthorization;
  return ((!active && !normalPaused) || legal.requires_reauthorization)
    && (!legal.flow.terms_event_id || !legal.flow.risk_event_id || legal.flow.stage === 'pre_mode');
}
export function customerJourney(model) {
  return deriveCustomerJourney({ status: model.status, pairing: model.pairing,
    desktopRuntimeReachable: model.desktopRuntimeReachable,
    connectionRecovery: model.connectionRecovery,
    desktopDownloadAvailable: secureExternalUrl(model.config.desktop_app_download_url) !== null,
    analysisReadiness: model.analysisReadiness,
    resumeAvailable: model.legal?.configured === true && !model.legal.requires_reauthorization,
    modeChoiceAvailable: modeChoiceAvailable(model) });
}
export function isPreview(status) {
  return status?.consent.mode === 'preview' || (status?.consent.mode === 'paused' && status.consent.resume_mode === 'preview');
}
export function desktopOwnsCapture(model) {
  const mode = model.status?.consent.mode;
  return (mode === 'full' || (mode === 'paused' && model.status?.consent.resume_mode === 'full'))
    && model.pairing?.desktop_control === true;
}
export function statusPresentation(model) {
  const status = model.status;
  const choice = (label, body) => ({ label, body });
  if (!status) return choice('Checking…', 'Checking the extension.');
  if (['preview', 'full'].includes(status.consent.mode) && status.observer?.helper === 'closed'
    && status.observer.attachment !== 'ready')
    return choice('Background tab closed', 'Reopen the background tab to receive activity.');
  if (status.phase === 'revoked' || status.consent.mode === 'revoked')
    return choice('Off', 'Analytics is off. Choose an analytics mode to start again.');
  if (status.phase === 'permission_required')
    return choice('Needs access', 'Allow site access so the extension can read activity from your creator account.');
  if (status.consent.mode === 'paused') return choice('Paused', desktopOwnsCapture(model) ? 'Pause and resume from the desktop app.' : 'New messages are not collected until you resume.');
  if (status.consent.mode === 'preview') return status.observer?.attachment === 'ready'
    ? choice('Ready', 'Preview counts update as you use OnlyFans.')
    : choice('Waiting for activity', 'Waiting for OnlyFans activity.');
  if (status.consent.mode === 'full' && ['pairing', 'compare'].includes(model.pairing?.state))
    return model.pairing.state === 'compare' ? choice('Confirm connection', 'Compare the codes in both places.')
      : choice('Connecting', 'Connecting to the desktop app.');
  if (status.consent.mode === 'full' && model.pairing?.state === 'unavailable')
    return model.connectionRecovery === 'retrying' ? choice('Reconnecting', 'Reconnecting to the extension.')
      : choice('Status unavailable', 'The extension connection could not be checked.');
  if (status.consent.mode === 'full' && !model.desktopRuntimeReachable)
    return ['authenticating', 'connecting'].includes(status.delivery?.transport_state) || model.connectionRecovery === 'retrying'
      ? choice('Connecting', 'Connecting to the desktop app.')
      : choice('Not connected', 'The desktop connection could not be confirmed.');
  if (model.pairing?.state === 'paired') {
    if (status.delivery?.transport_state !== 'authenticated') return ['authenticating', 'connecting'].includes(status.delivery?.transport_state)
      ? choice('Connecting', 'Connecting to the desktop app.')
      : choice('Not connected', 'The desktop connection could not be confirmed.');
    if (model.analysisReadiness?.commercial_authority === 'required') return choice('Activation needed', 'Turn on Full analytics in the desktop app.');
    if (model.analysisReadiness?.commercial_authority === 'unavailable') return choice('Needs attention', 'Activation could not be checked.');
    if (model.analysisReadiness?.commercial_authority !== 'active') return choice('Checking activation', '');
    if (status.delivery?.browser_tab_sleeping === true)
      return choice('Browser tab paused', 'Open OnlyFans to continue.');
    if (model.analysisReadiness?.analysis_admission === 'blocked') return choice('Not ready', "New messages aren't being analyzed.");
    if (model.analysisReadiness?.analysis_admission !== 'admitted') return choice('Checking analysis', '');
  }
  if (status.consent.mode !== 'full' || model.pairing?.state !== 'paired') return choice('Not connected', 'Choose Connect extension in the desktop app to finish.');
  return choice('Ready', 'Keep your creator tab open so new messages can arrive.');
}
export function readinessLabels(model) {
  if (model.status?.consent.mode !== 'full') return { connection: 'Not in use', activation: 'Not in use', analysis: 'Not in use' };
  const connected = model.desktopRuntimeReachable && model.pairing.state === 'paired'
    && model.status.delivery?.transport_state === 'authenticated';
  return { connection: connected ? 'Connected' : 'Not connected',
    activation: ({ active: 'Active', required: 'Required', unavailable: 'Needs attention', unknown: connected ? 'Checking…' : 'Not checked' })[model.analysisReadiness.commercial_authority] ?? 'Checking…',
    analysis: statusPresentation(model).label === 'Ready'
      ? 'Ready' : model.analysisReadiness.commercial_authority === 'required' ? 'Waiting for activation' : 'Not ready' };
}
