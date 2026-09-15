import {
  CAPTURE_DELIVERY_TYPE,
  CAPTURE_DELIVERY_VERSION,
  CAPTURE_MESSAGE_TYPE,
  PAGE_CONTROL_MESSAGE_TYPE,
  PAGE_CONTROL_VERSION,
  PREVIEW_MESSAGE_TYPE,
  isCaptureEnvelope,
  isPreviewEnvelope,
  isProvisioningIdentityEnvelope,
} from './capture/envelopes.mjs';
import {
  CAPTURE_LIMITS,
  CaptureDeliveryQueue,
  utf8Bytes,
} from './capture/delivery-queue.mjs';

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

  function reportDeliveryFailure(reason = 'runtime_delivery_failed') {
    deliveryFailureCount += 1;
    console.warn('[Conversation Analytics] local observation delivery failed', {
      reason,
      count: deliveryFailureCount,
    });
  }

  function sendRuntimeMessage(message, signal = null) {
    return new Promise((resolve, reject) => {
      signal?.throwIfAborted();
      let settled = false;
      const timeout = setTimeout(() => {
        if (settled) return;
        settled = true;
        signal?.removeEventListener('abort', abort);
        reject(Object.assign(new Error('runtime_delivery_timeout'), {
          code: 'runtime_delivery_timeout',
        }));
      }, 5_000);
      const abort = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        reject(signal.reason ?? new DOMException('Aborted', 'AbortError'));
      };
      signal?.addEventListener('abort', abort, { once: true });
      try {
        chrome.runtime.sendMessage(message, (response) => {
          if (settled) return;
          settled = true;
          clearTimeout(timeout);
          signal?.removeEventListener('abort', abort);
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
            return;
          }
          resolve(response);
        });
      } catch (error) {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        signal?.removeEventListener('abort', abort);
        reject(error);
      }
    });
  }

  let consentEpoch = null;
  const pendingContextCaptures = [];
  let pendingContextBytes = 0;
  const deliveryQueue = new CaptureDeliveryQueue({
    send: (delivery, signal) => sendRuntimeMessage(delivery, signal),
  });

  function enqueueCaptureDelivery(delivery) {
    try {
      void deliveryQueue.enqueue(delivery).catch((error) => {
        reportDeliveryFailure(error?.code ?? 'runtime_delivery_failed');
      });
    } catch (error) {
      reportDeliveryFailure(error?.code ?? 'delivery_queue_full');
    }
  }

  function makeCaptureDelivery(envelope, createdAtMs = Date.now(), deliveryId = crypto.randomUUID()) {
    return {
      type: CAPTURE_DELIVERY_TYPE,
      version: CAPTURE_DELIVERY_VERSION,
      delivery_id: deliveryId,
      created_at_ms: createdAtMs,
      consent_epoch: consentEpoch,
      observation: envelope.observation,
    };
  }

  function bufferUntilContext(envelope) {
    const bytes = utf8Bytes(JSON.stringify(envelope));
    if (
      pendingContextCaptures.length >= CAPTURE_LIMITS.queueEntries
      || pendingContextBytes + bytes > CAPTURE_LIMITS.queueBytes
    ) {
      reportDeliveryFailure('delivery_queue_full');
      return;
    }
    pendingContextCaptures.push({
      envelope: structuredClone(envelope),
      bytes,
      createdAtMs: Date.now(),
      deliveryId: crypto.randomUUID(),
    });
    pendingContextBytes += bytes;
  }

  function settlePendingContextCaptures(available) {
    const pending = pendingContextCaptures.splice(0);
    pendingContextBytes = 0;
    if (!available || consentEpoch === null) {
      for (const _entry of pending) reportBridgeDrop('capture_context_unavailable');
      return;
    }
    for (const entry of pending) {
      enqueueCaptureDelivery(
        makeCaptureDelivery(entry.envelope, entry.createdAtMs, entry.deliveryId),
      );
    }
  }

  void sendRuntimeMessage({ type: 'ofca.capture.context.query' }).then((response) => {
    if (!active) return;
    if (response?.ok === true && typeof response.consent_epoch === 'string') {
      consentEpoch = response.consent_epoch;
      settlePendingContextCaptures(true);
      return;
    }
    settlePendingContextCaptures(false);
  }, () => {
    if (!active) return;
    reportDeliveryFailure('capture_context_unavailable');
    settlePendingContextCaptures(false);
  });

  function forwardRuntimeMessage(message, isDeliveryFailure) {
    void sendRuntimeMessage(message).then(
      (response) => {
        if (isDeliveryFailure(response)) reportDeliveryFailure();
      },
      () => reportDeliveryFailure(),
    );
  }

  function pageMessageListener(event) {
    if (!active || event.source !== window || event.origin !== pageOrigin) return;
    const envelope = event.data;
    if (envelope?.type === CAPTURE_MESSAGE_TYPE) {
      if (!isCaptureEnvelope(envelope)) {
        reportBridgeDrop('invalid_capture_envelope');
        return;
      }
      if (consentEpoch === null) {
        bufferUntilContext(envelope);
        return;
      }
      enqueueCaptureDelivery(makeCaptureDelivery(envelope));
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
    pendingContextCaptures.length = 0;
    pendingContextBytes = 0;
    deliveryQueue.close('capture_stopped');
    window.removeEventListener('message', pageMessageListener);
    window.removeEventListener('pagehide', stop);
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
  window.addEventListener('pagehide', stop);
})();