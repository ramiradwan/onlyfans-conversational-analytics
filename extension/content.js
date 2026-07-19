(function installCaptureBridge() {
  if (globalThis.__OFCA_CAPTURE_BRIDGE_ACTIVE__) return;
  globalThis.__OFCA_CAPTURE_BRIDGE_ACTIVE__ = true;

  const CAPTURE_MESSAGE_TYPE = 'ofca.capture.observation';
  const PROTOCOL_VERSION = '2';
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

  window.addEventListener('message', (event) => {
    if (event.source !== window || event.origin !== pageOrigin) return;

    const envelope = event.data;
    if (!isRecord(envelope) || envelope.type !== CAPTURE_MESSAGE_TYPE) return;
    if (
      envelope.protocol_version !== PROTOCOL_VERSION
      || !hasExactKeys(envelope, ['type', 'protocol_version', 'observation'])
    ) {
      reportBridgeDrop('invalid_page_envelope');
      return;
    }

    try {
      chrome.runtime.sendMessage(
        {
          type: CAPTURE_MESSAGE_TYPE,
          protocol_version: PROTOCOL_VERSION,
          observation: envelope.observation,
        },
        (response) => {
          if (chrome.runtime.lastError || response?.retryable === true) {
            reportDeliveryFailure();
          }
        },
      );
    } catch (_error) {
      reportDeliveryFailure();
    }
  });
})();
