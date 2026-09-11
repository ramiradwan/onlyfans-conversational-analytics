const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const CONTEXT_ERROR_CODES = new Set([
  'account_mismatch',
  'capture_disabled',
  'identity_required',
  'invalid_sender',
  'stale_capture_context',
]);

function captureContextError(code) {
  const error = new Error(code);
  error.code = code;
  error.retryable = false;
  return error;
}

export function isCaptureEpoch(value) {
  return typeof value === 'string' && UUID_V4.test(value);
}

export function isCaptureContextError(error) {
  return CONTEXT_ERROR_CODES.has(error?.code);
}

export function captureSenderKey(sender, extensionId) {
  let origin = null;
  try {
    origin = typeof sender?.url === 'string' ? new URL(sender.url).origin : null;
  } catch (_error) {
    origin = null;
  }
  if (
    sender?.id !== extensionId
    || sender?.frameId !== 0
    || !Number.isInteger(sender?.tab?.id)
    || typeof sender?.documentId !== 'string'
    || sender.documentId.length === 0
    || origin !== 'https://onlyfans.com'
    || (
      sender.documentLifecycle !== undefined
      && sender.documentLifecycle !== 'active'
    )
  ) throw captureContextError('invalid_sender');
  return `${sender.tab.id}:${sender.documentId}`;
}

export function assertCaptureContext({
  sender,
  extensionId,
  context,
  configuration,
  boundCreatorAccountId,
  observation,
  consent,
  delivery,
}) {
  const senderKey = captureSenderKey(sender, extensionId);
  if (consent?.mode !== 'full') throw captureContextError('capture_disabled');
  if (
    typeof boundCreatorAccountId !== 'string'
    || boundCreatorAccountId.length === 0
    || configuration?.creator_account_id !== boundCreatorAccountId
  ) throw captureContextError('account_mismatch');

  const expectedPlatformId = configuration
    ?.history_acquisition
    ?.authorized_platform_creator_id;
  if (typeof expectedPlatformId !== 'string' || expectedPlatformId.length === 0) {
    throw captureContextError('identity_required');
  }
  if (
    !context
    || context.identity_conflict === true
    || !isCaptureEpoch(consent.consent_epoch)
    || context.consent_epoch !== consent.consent_epoch
    || delivery?.consent_epoch !== consent.consent_epoch
    || context.sender_key !== senderKey
    || !isCaptureEpoch(context.page_epoch)
    || !isCaptureEpoch(observation?.page_epoch)
    || context.page_epoch !== observation.page_epoch
  ) throw captureContextError('stale_capture_context');
  if (
    context.observed_platform_id !== expectedPlatformId
    || observation.creator_platform_user_id !== expectedPlatformId
  ) throw captureContextError('account_mismatch');
  return expectedPlatformId;
}
