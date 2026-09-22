import { CAPTURE_LIMITS, fitsUtf8 } from './limits.mjs';

export const CAPTURE_MESSAGE_TYPE = 'ofca.capture.observation';
export const CAPTURE_PROTOCOL_VERSION = '2';
export const CAPTURE_DELIVERY_TYPE = 'ofca.capture.delivery';
export const CAPTURE_DELIVERY_VERSION = 1;
export const PREVIEW_MESSAGE_TYPE = 'ofca.preview.observation';
export const PREVIEW_PROTOCOL_VERSION = 2;
export const PAGE_CONTROL_MESSAGE_TYPE = 'ofca.capture.control';
export const PAGE_CONTROL_VERSION = 1;
export const PROVISIONING_IDENTITY_MESSAGE_TYPE = 'ofca.provisioning.identity.update';
export const PROVISIONING_IDENTITY_RESET_TYPE = 'ofca.provisioning.identity.reset';
export const PROVISIONING_IDENTITY_VERSION = 1;

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasExactKeys(value, expected) {
  const keys = Object.keys(value);
  return keys.length === expected.length && keys.every((key) => expected.includes(key));
}

function isIdentifier(value) {
  return typeof value === 'string' && value.length >= 1 && value.length <= 200;
}

function isTimestamp(value) {
  return typeof value === 'string' && Number.isFinite(Date.parse(value));
}

function isPageEpoch(value) {
  return typeof value === 'string' && UUID_V4.test(value);
}

function isSourcePath(value) {
  return typeof value === 'string'
    && value.startsWith('/')
    && value.length <= 2048;
}

function isChatRecord(value) {
  return isRecord(value)
    && hasExactKeys(value, ['chat_id', 'platform_user_id', 'display_name', 'updated_at'])
    && isIdentifier(value.chat_id)
    && isIdentifier(value.platform_user_id)
    && (value.display_name === null || fitsUtf8(value.display_name, CAPTURE_LIMITS.displayNameBytes))
    && isTimestamp(value.updated_at);
}

function isMessageRecord(value) {
  return isRecord(value)
    && hasExactKeys(value, [
      'message_id',
      'chat_id',
      'sender_platform_user_id',
      'text',
      'sent_at',
      'direction',
    ])
    && isIdentifier(value.message_id)
    && isIdentifier(value.chat_id)
    && isIdentifier(value.sender_platform_user_id)
    && fitsUtf8(value.text, CAPTURE_LIMITS.textBytes)
    && isTimestamp(value.sent_at)
    && ['inbound', 'outbound'].includes(value.direction);
}

export function isCaptureObservation(value) {
  if (!isRecord(value)) return false;
  if (value.event_type === 'hook.diagnostic') {
    return hasExactKeys(value, [
      'event_type',
      'source_event_type',
      'code',
      'observed_at',
      'source_path',
    ])
      && ['http.response', 'websocket.message'].includes(value.source_event_type)
      && ['invalid_json', 'unrecognized_payload', 'capture_too_large'].includes(value.code)
      && isTimestamp(value.observed_at)
      && isSourcePath(value.source_path);
  }
  if (!hasExactKeys(value, [
    'event_type',
    'observed_at',
    'source_path',
    'creator_platform_user_id',
    'context_chat_id',
    'page_epoch',
    'record',
  ])) return false;
  if (
    !isTimestamp(value.observed_at)
    || !isSourcePath(value.source_path)
    || !isPageEpoch(value.page_epoch)
    || (value.creator_platform_user_id !== null && !isIdentifier(value.creator_platform_user_id))
    || (value.context_chat_id !== null && !isIdentifier(value.context_chat_id))
  ) return false;
  if (value.event_type === 'chat.observed') return isChatRecord(value.record);
  if (value.event_type === 'message.observed') return isMessageRecord(value.record);
  return false;
}

export function isCaptureEnvelope(value) {
  return isRecord(value)
    && hasExactKeys(value, ['type', 'protocol_version', 'observation'])
    && value.type === CAPTURE_MESSAGE_TYPE
    && value.protocol_version === CAPTURE_PROTOCOL_VERSION
    && isCaptureObservation(value.observation)
    && fitsUtf8(JSON.stringify(value), CAPTURE_LIMITS.envelopeBytes);
}

export function isCaptureDelivery(value) {
  return isRecord(value)
    && hasExactKeys(value, ['type', 'version', 'delivery_id', 'created_at_ms', 'consent_epoch', 'observation'])
    && value.type === CAPTURE_DELIVERY_TYPE
    && value.version === CAPTURE_DELIVERY_VERSION
    && isPageEpoch(value.delivery_id)
    && isPageEpoch(value.consent_epoch)
    && Number.isSafeInteger(value.created_at_ms)
    && value.created_at_ms >= 0
    && isCaptureObservation(value.observation)
    && fitsUtf8(JSON.stringify(value), CAPTURE_LIMITS.envelopeBytes);
}

export function isPreviewObservation(value) {
  if (!isRecord(value) || !isTimestamp(value.observed_at) || !isTimestamp(value.activity_at)
    || !isIdentifier(value.creator_id) || !isIdentifier(value.record_id)) return false;
  if (value.kind === 'chat') {
    return hasExactKeys(value, ['kind', 'observed_at', 'activity_at', 'creator_id', 'record_id']);
  }
  return value.kind === 'message'
    && hasExactKeys(value, ['kind', 'observed_at', 'activity_at', 'creator_id', 'record_id', 'chat_id', 'direction'])
    && isIdentifier(value.chat_id)
    && ['inbound', 'outbound', 'unknown'].includes(value.direction);
}

export function isPreviewEnvelope(value) {
  return isRecord(value)
    && hasExactKeys(value, ['type', 'version', 'observation'])
    && value.type === PREVIEW_MESSAGE_TYPE
    && value.version === PREVIEW_PROTOCOL_VERSION
    && isPreviewObservation(value.observation);
}

export function isProvisioningIdentityEnvelope(value) {
  if (isRecord(value) && value.type === PROVISIONING_IDENTITY_RESET_TYPE) {
    return hasExactKeys(value, ['type', 'version', 'page_epoch'])
      && value.version === PROVISIONING_IDENTITY_VERSION && isPageEpoch(value.page_epoch);
  }
  if (
    !isRecord(value)
    || !hasExactKeys(value, ['type', 'version', 'page_epoch', 'authenticated_profile'])
    || value.type !== PROVISIONING_IDENTITY_MESSAGE_TYPE
    || value.version !== PROVISIONING_IDENTITY_VERSION
    || !isPageEpoch(value.page_epoch)
  ) return false;
  if (value.authenticated_profile === null) return true;
  return isRecord(value.authenticated_profile)
    && hasExactKeys(value.authenticated_profile, ['creator_account_id'])
    && isIdentifier(value.authenticated_profile.creator_account_id);
}
