// Closed, bounded Product-owned browser task messages. No message grants consent.
export const PORT = 'ofca.browser-task.v1';
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
export const EXTENSION = /^[a-p]{32}$/u;
const DIGEST = /^[a-f0-9]{64}$/u;
export const PURPOSES = ['browser.access', 'browser.identity', 'browser.authentication', 'browser.pairing-confirmation', 'browser.history-permission'];
export const STATES = ['unavailable', 'needs-consent', 'needs-permission', 'needs-account', 'ready', 'paused'];
export const exact = (value, keys) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const generation = value => Number.isSafeInteger(value) && value >= 0;
export function isTask(value) {
  return exact(value, ['task_id', 'journey_id', 'operation_id', 'purpose', 'scope_digest', 'source_revision', 'admission_generation', 'state', 'expires_at'])
    && ['task_id', 'journey_id', 'operation_id', 'admission_generation'].every(key => UUID.test(value[key]))
    && PURPOSES.includes(value.purpose) && DIGEST.test(value.scope_digest) && generation(value.source_revision)
    && ['pending', 'accepted', 'outcome_unknown', 'completed', 'cancelled', 'expired'].includes(value.state)
    && typeof value.expires_at === 'number' && Number.isFinite(value.expires_at) && value.expires_at > 0;
}
export function isParticipant(value) {
  return exact(value, ['instance_id', 'draft_scope_id', 'disclosure_bundle_id'])
    && UUID.test(value.instance_id) && UUID.test(value.draft_scope_id) && DIGEST.test(value.disclosure_bundle_id);
}
export function isPayload(value) {
  return payloadEnvelope(value)
    && (value.task.purpose === 'browser.authentication' ? isCeremony(value.ceremony) : value.ceremony === null);
}
// Brain may report completed work after withdrawing its launch authority. This
// projection is never accepted as an extension attachment or dispatch payload.
export function isPayloadProjection(value) {
  return isPayload(value) || payloadEnvelope(value) && value.task.state === 'completed' && value.ceremony === null;
}
function payloadEnvelope(value) {
  return exact(value, ['profile', 'task', 'extension_id', 'ceremony', 'entry_source', 'participant', 'creator_account_id'])
    && value.profile === 'browser-task.v1' && isTask(value.task) && EXTENSION.test(value.extension_id)
    && ['desktop', 'extension'].includes(value.entry_source) && (value.participant === null || isParticipant(value.participant))
    && (value.creator_account_id === null || typeof value.creator_account_id === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u.test(value.creator_account_id));
}
function isCeremony(value) {
  return exact(value, ['entry_url', 'ceremony_id', 'attempt_id', 'completion', 'expires_at',
    ...(value?.preparation_recovery === true ? ['preparation_recovery'] : [])])
    && typeof value.entry_url === 'string' && value.entry_url.length <= 2048
    && /^https:\/\/[^/?#@]+\/onboarding\/browser-ceremonies\/[A-Za-z0-9_-]{43}$/u.test(value.entry_url)
    && /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(value.ceremony_id)
    && /^[A-Za-z0-9_-]{43}$/u.test(value.attempt_id)
    && typeof value.expires_at === 'string' && value.expires_at.length <= 40 && Number.isFinite(Date.parse(value.expires_at))
    && (exact(value.completion, ['mode']) && value.completion.mode === 'system-browser'
      || exact(value.completion, ['mode', 'return_uri']) && value.completion.mode === 'extension-auth-flow'
        && /^https:\/\/[a-p]{32}\.chromiumapp\.org\/browser-ceremony\/v1\/complete$/u.test(value.completion.return_uri));
}
export function isObservation(value) {
  const labels = value && ['creator_display_name', 'creator_username'].filter(field => Object.hasOwn(value, field));
  const validLabels = labels?.every(field => value.creator_account_id !== null && typeof value[field] === 'string'
    && value[field].length > 0 && [...value[field]].length <= (field === 'creator_display_name' ? 80 : 128)
    && value[field] === value[field].trim() && !/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/u.test(value[field]));
  if (!validLabels) return false;
  return exact(value, ['profile', 'task_id', 'journey_id', 'admission_generation', 'extension_id', 'instance_id', 'epoch', 'revision',
    'account_generation', 'consent_generation', 'draft_scope_id', 'disclosure_bundle_id', 'state', 'creator_account_id', ...labels])
    && value.profile === 'browser-task-observation.v1'
    && ['task_id', 'journey_id', 'admission_generation', 'instance_id', 'epoch', 'draft_scope_id'].every(key => UUID.test(value[key]))
    && ['revision', 'account_generation', 'consent_generation'].every(key => generation(value[key]))
    && EXTENSION.test(value.extension_id) && DIGEST.test(value.disclosure_bundle_id) && STATES.includes(value.state)
    && (['ready', 'paused'].includes(value.state)
      ? typeof value.creator_account_id === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u.test(value.creator_account_id)
      : value.creator_account_id === null);
}
export function sameTask(value, task) {
  return ['task_id', 'journey_id', 'admission_generation'].every(key => value[key] === task[key]);
}
export function isAuthentication(value, payload) {
  const notification = value?.notification;
  return payload.task.purpose === 'browser.authentication'
    && exact(value, ['type', 'task_id', 'admission_generation', 'state', 'notification']) && value.type === 'authentication'
    && value.task_id === payload.task.task_id && value.admission_generation === payload.task.admission_generation
    && ['idle', 'opening', 'checking', 'closed'].includes(value.state)
    && (value.state === 'checking' ? exact(notification, ['ceremony_id', 'attempt_id', 'notification'])
      && notification.ceremony_id === payload.ceremony.ceremony_id && notification.attempt_id === payload.ceremony.attempt_id
      && ['changed', 'retired', 'unavailable'].includes(notification.notification) : notification === null);
}
export function isBrowserTaskControl(value) {
  return exact(value, ['type', 'id', 'action', 'payload']) && value.type === 'session.control'
    && value.action === 'browser.task' && isPayload(value.payload)
    && value.id === value.payload.task.task_id && value.payload.creator_account_id !== null
    && value.payload.task.purpose === 'browser.authentication';
}
export function isPairingComparison(value, payload) {
  return payload.task.purpose === 'browser.pairing-confirmation'
    && exact(value, ['type', 'task_id', 'admission_generation', 'state', 'comparison_code'])
    && value.type === 'pairing' && value.state === 'compare'
    && value.task_id === payload.task.task_id && value.admission_generation === payload.task.admission_generation
    && typeof value.comparison_code === 'string' && /^[0-9]{6}$/u.test(value.comparison_code);
}
export function isSavedPairing(value, payload) {
  return payload.task.purpose === 'browser.pairing-confirmation'
    && exact(value, ['type', 'task_id', 'admission_generation']) && value.type === 'pairing-saved'
    && value.task_id === payload.task.task_id && value.admission_generation === payload.task.admission_generation;
}
export function isPairingCompleted(value, payload) {
  return payload.task.purpose === 'browser.pairing-confirmation'
    && exact(value, ['type', 'task_id', 'admission_generation']) && value.type === 'pairing-completed'
    && value.task_id === payload.task.task_id && value.admission_generation === payload.task.admission_generation;
}
export function helperGeneration(value, origin = 'http://bridge.localhost:17871') {
  try {
    const url = new URL(value), fields = new URLSearchParams(url.hash.slice(1));
    const id = fields.get('presentation');
    return fields.size === 1 && UUID.test(id) && url.href === `${origin}/browser-tasks#presentation=${id}` ? id : null;
  } catch { return null; }
}
