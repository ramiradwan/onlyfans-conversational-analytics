export const CAPTURE_MESSAGE_TYPE = 'ofca.capture.observation';
export const CAPTURE_PROTOCOL_VERSION = '2';
export const PREVIEW_MESSAGE_TYPE = 'ofca.preview.observation';
export const PREVIEW_PROTOCOL_VERSION = 1;
export const PAGE_CONTROL_MESSAGE_TYPE = 'ofca.capture.control';
export const PAGE_CONTROL_VERSION = 1;
export const PROVISIONING_IDENTITY_MESSAGE_TYPE = 'ofca.provisioning.identity.update';
export const PROVISIONING_IDENTITY_VERSION = 1;

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
    && (value.display_name === null || typeof value.display_name === 'string')
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
    && typeof value.text === 'string'
    && isTimestamp(value.sent_at)
    && ['inbound', 'outbound'].includes(value.direction);
}

function isCaptureObservation(value) {
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
      && ['invalid_json', 'unrecognized_payload'].includes(value.code)
      && isTimestamp(value.observed_at)
      && isSourcePath(value.source_path);
  }
  if (!hasExactKeys(value, [
    'event_type',
    'observed_at',
    'source_path',
    'creator_platform_user_id',
    'context_chat_id',
    'record',
  ])) return false;
  if (
    !isTimestamp(value.observed_at)
    || !isSourcePath(value.source_path)
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
    && isCaptureObservation(value.observation);
}

export function isPreviewObservation(value) {
  if (!isRecord(value) || !isTimestamp(value.observed_at)) return false;
  if (value.kind === 'chat') {
    return hasExactKeys(value, ['kind', 'observed_at']);
  }
  return value.kind === 'message'
    && hasExactKeys(value, ['kind', 'observed_at', 'direction'])
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
  if (
    !isRecord(value)
    || !hasExactKeys(value, ['type', 'version', 'authenticated_profile'])
    || value.type !== PROVISIONING_IDENTITY_MESSAGE_TYPE
    || value.version !== PROVISIONING_IDENTITY_VERSION
  ) return false;
  if (value.authenticated_profile === null) return true;
  return isRecord(value.authenticated_profile)
    && hasExactKeys(value.authenticated_profile, ['creator_account_id'])
    && isIdentifier(value.authenticated_profile.creator_account_id);
}
