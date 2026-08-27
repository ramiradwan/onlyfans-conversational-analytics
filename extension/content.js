import {
  CAPTURE_MESSAGE_TYPE,
  PAGE_CONTROL_MESSAGE_TYPE,
  PAGE_CONTROL_VERSION,
  PREVIEW_MESSAGE_TYPE,
  isCaptureEnvelope,
  isPreviewEnvelope,
  isProvisioningIdentityEnvelope,
} from './capture/envelopes.mjs';

(function installCaptureBridge() {
  if (globalThis.__OFCA_CAPTURE_BRIDGE_ACTIVE__) return;
  globalThis.__OFCA_CAPTURE_BRIDGE_ACTIVE__ = true;

  const pageOrigin = window.location.origin;
  let active = true;
  let droppedEnvelopeCount = 0;
  let deliveryFailureCount = 0;

  function reportBridgeDrop(reason) {
    droppedEnvelopeCount += 1;
    console.warn('[Conversation Analytics] page observation rejected', {
      reason,
      count: droppedEnvelopeCount,
    });
  }

  function reportDeliveryFailure() {
    deliveryFailureCount += 1;
    console.warn('[Conversation Analytics] local observation delivery failed', {
      reason: 'runtime_delivery_failed',
      count: deliveryFailureCount,
    });
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

  function pageMessageListener(event) {
    if (!active || event.source !== window || event.origin !== pageOrigin) return;
    const envelope = event.data;
    if (envelope?.type === CAPTURE_MESSAGE_TYPE) {
      if (!isCaptureEnvelope(envelope)) {
        reportBridgeDrop('invalid_capture_envelope');
        return;
      }
      forwardRuntimeMessage(envelope, (response) => response?.retryable === true);
      return;
    }
    if (envelope?.type === PREVIEW_MESSAGE_TYPE) {
      if (!isPreviewEnvelope(envelope)) {
        reportBridgeDrop('invalid_preview_envelope');
        return;
      }
      forwardRuntimeMessage(envelope, (response) => response?.ok !== true);
      return;
    }
    if (!isProvisioningIdentityEnvelope(envelope)) {
      if (envelope?.type === 'ofca.provisioning.identity.update') {
        reportBridgeDrop('invalid_identity_envelope');
      }
      return;
    }
    forwardRuntimeMessage(envelope, (response) => response?.ok !== true);
  }

  function stop() {
    if (!active) return;
    active = false;
    window.removeEventListener('message', pageMessageListener);
    window.postMessage({
      type: PAGE_CONTROL_MESSAGE_TYPE,
      version: PAGE_CONTROL_VERSION,
      action: 'stop',
    }, pageOrigin);
    delete globalThis.__OFCA_CAPTURE_BRIDGE_ACTIVE__;
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== PAGE_CONTROL_MESSAGE_TYPE || message.action !== 'stop') return false;
    stop();
    sendResponse?.({ ok: true });
    return false;
  });
  window.addEventListener('message', pageMessageListener);
})();
