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
    desktopDownloadAvailable: secureExternalUrl(model.config.desktop_app_download_url) !== null,
    analysisReadiness: model.analysisReadiness,
    resumeAvailable: model.legal?.configured === true && !model.legal.requires_reauthorization,
    modeChoiceAvailable: modeChoiceAvailable(model) });
}
export function isPreview(status) {
  return status?.consent.mode === 'preview' || (status?.consent.mode === 'paused' && status.consent.resume_mode === 'preview');
}
export function readinessLabels(model) {
  if (model.status?.consent.mode !== 'full') return { connection: 'Not in use', activation: 'Not in use', analysis: 'Not in use' };
  const connected = model.desktopRuntimeReachable && model.pairing.state === 'paired'
    && model.status.delivery?.transport_state === 'authenticated';
  return { connection: connected ? 'Connected' : 'Not connected',
    activation: ({ active: 'Active', required: 'Required', unavailable: 'Needs attention', unknown: connected ? 'Checking…' : 'Not checked' })[model.analysisReadiness.commercial_authority] ?? 'Checking…',
    analysis: connected && model.analysisReadiness.commercial_authority === 'active' && model.analysisReadiness.analysis_admission === 'admitted'
      ? 'Ready' : model.analysisReadiness.commercial_authority === 'required' ? 'Waiting for activation' : 'Not ready' };
}
export function journeyBadge(journey) {
  if (journey.tone === 'success') return 'Ready';
  if (journey.tone === 'error') return 'Needs attention';
  if (journey.id.startsWith('activation_') && journey.tone === 'progress') return 'Checking';
  if (journey.tone === 'progress') return 'Connecting';
  return journey.tone === 'warning' ? 'Needs attention' : '';
}
