(function installCaptureBridge() {
  if (globalThis.__OFCA_CAPTURE_BRIDGE_ACTIVE__) return;
  globalThis.__OFCA_CAPTURE_BRIDGE_ACTIVE__ = true;

  const CAPTURE_MESSAGE_TYPE = 'ofca.capture.observation';
  const PROTOCOL_VERSION = '2';
  const PROVISIONING_IDENTITY_MESSAGE_TYPE = 'ofca.provisioning.identity.update';
  const PROVISIONING_IDENTITY_VERSION = 1;
  const pageOrigin = window.location.origin;
  let droppedEnvelopeCount = 0;
  let deliveryFailureCount = 0;

  function isRecord(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
  }

  function hasExactKeys(value, expected) {
    const keys = Object.keys(value);
    return keys.length === expected.length && keys.every((key) => expected.includes(key));
  }

  function reportBridgeDrop(reason) {
    droppedEnvelopeCount += 1;
    console.warn('[Agent] capture bridge dropped an envelope', {
      reason,
      count: droppedEnvelopeCount,
    });
  }

  function reportDeliveryFailure() {
    deliveryFailureCount += 1;
    console.warn('[Agent] capture bridge delivery failed', {
      reason: 'runtime_delivery_failed',
      count: deliveryFailureCount,
    });
  }

  function isProvisioningIdentityEnvelope(envelope) {
    if (
      !isRecord(envelope)
      || !hasExactKeys(envelope, ['type', 'version', 'authenticated_profile'])
      || envelope.type !== PROVISIONING_IDENTITY_MESSAGE_TYPE
      || envelope.version !== PROVISIONING_IDENTITY_VERSION
    ) return false;
    if (envelope.authenticated_profile === null) return true;
    const profile = envelope.authenticated_profile;
    return isRecord(profile)
      && hasExactKeys(profile, ['creator_account_id'])
      && typeof profile.creator_account_id === 'string'
      && profile.creator_account_id.length >= 1
      && profile.creator_account_id.length <= 200;
  }

  function forwardRuntimeMessage(message, isDeliveryFailure) {
    try {
      chrome.runtime.sendMessage(message, (response) => {
        if (chrome.runtime.lastError || isDeliveryFailure(response)) reportDeliveryFailure();
      });
    } catch (_error) {
      reportDeliveryFailure();
    }
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window || event.origin !== pageOrigin) return;

    const envelope = event.data;
    if (!isRecord(envelope)) return;
    if (envelope.type === CAPTURE_MESSAGE_TYPE) {
      if (
        envelope.protocol_version !== PROTOCOL_VERSION
        || !hasExactKeys(envelope, ['type', 'protocol_version', 'observation'])
      ) {
        reportBridgeDrop('invalid_page_envelope');
        return;
      }

      forwardRuntimeMessage({
        type: CAPTURE_MESSAGE_TYPE,
        protocol_version: PROTOCOL_VERSION,
        observation: envelope.observation,
      }, (response) => response?.retryable === true);
      return;
    }

    if (envelope.type !== PROVISIONING_IDENTITY_MESSAGE_TYPE) return;
    if (!isProvisioningIdentityEnvelope(envelope)) {
      reportBridgeDrop('invalid_provisioning_identity_envelope');
      return;
    }
    forwardRuntimeMessage(envelope, (response) => response?.ok !== true);
  });
})();
