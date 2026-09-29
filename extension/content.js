import {
  CAPTURE_DELIVERY_TYPE,
  CAPTURE_DELIVERY_VERSION,
  CAPTURE_MESSAGE_TYPE,
  PAGE_CONTROL_MESSAGE_TYPE,
  PAGE_CONTROL_VERSION,
  PAGE_CONTROL_STATUS_TYPE,
  CAPTURE_STATE_QUERY_TYPE,
  PREVIEW_MESSAGE_TYPE,
  isCaptureEnvelope,
  isPreviewEnvelope,
  isProvisioningIdentityEnvelope,
} from './capture/envelopes.mjs';
import { CaptureDeliveryQueue } from './capture/delivery-queue.mjs';

(function installCaptureBridge() {
  if (globalThis.__OFCA_CAPTURE_BRIDGE_ACTIVE__) return;
  globalThis.__OFCA_CAPTURE_BRIDGE_ACTIVE__ = true;

  const pageOrigin = window.location.origin;
  let active = true;
  let forwarding = false;
  let stateGeneration = 0;
  const statusWaiters = new Set();
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
  const createQueue = () => new CaptureDeliveryQueue({
    send: (delivery, signal) => sendRuntimeMessage(delivery, signal),
  });
  let deliveryQueue = createQueue();

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

  function pageControl(action) {
    window.postMessage({ type: PAGE_CONTROL_MESSAGE_TYPE, version: PAGE_CONTROL_VERSION, action }, pageOrigin);
  }

  function pause() {
    forwarding = false;
    consentEpoch = null;
    stateGeneration += 1;
    deliveryQueue.close('capture_paused');
    pageControl('pause');
  }

  async function resume() {
    if (!active) return;
    const generation = ++stateGeneration;
    try {
      const response = await sendRuntimeMessage({ type: CAPTURE_STATE_QUERY_TYPE });
      if (!active || generation !== stateGeneration) return;
      if (response?.ok !== true || !['full', 'preview'].includes(response.mode)
        || typeof response.consent_epoch !== 'string') { pause(); return; }
      if (consentEpoch !== response.consent_epoch || !forwarding) {
        pause();
        deliveryQueue = createQueue();
      }
      consentEpoch = response.consent_epoch;
      forwarding = true;
      pageControl('resume');
      pageControl('refresh_identity');
    } catch {
      if (active && generation === stateGeneration) {
        pause();
        reportDeliveryFailure('capture_context_unavailable');
      }
    }
  }

  function forwardRuntimeMessage(message, isDeliveryFailure) {
    void sendRuntimeMessage(message).then(
      (response) => {
        if (isDeliveryFailure(response)) reportDeliveryFailure();
      },
      () => reportDeliveryFailure(),
    );
  }

  function pageMessageListener(event) {
    if (event.source !== window || event.origin !== pageOrigin) return;
    const envelope = event.data;
    if (envelope?.type === PAGE_CONTROL_STATUS_TYPE && envelope.version === PAGE_CONTROL_VERSION) {
      if (active && forwarding && envelope.status?.active === true && envelope.status.forwarding === false) {
        pageControl('resume');
        pageControl('refresh_identity');
      }
      for (const resolve of [...statusWaiters]) resolve(envelope.status);
      return;
    }
    if (!active || !forwarding) return;
    if (envelope?.type === CAPTURE_MESSAGE_TYPE) {
      if (!isCaptureEnvelope(envelope)) {
        reportBridgeDrop('invalid_capture_envelope');
        return;
      }
      if (consentEpoch === null) return;
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
    pause();
    active = false;
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
    if (message?.type !== PAGE_CONTROL_MESSAGE_TYPE) return false;
    if (message.action === 'status' && message.version === PAGE_CONTROL_VERSION) {
      let timer;
      const finish = (status) => {
        clearTimeout(timer);
        statusWaiters.delete(finish);
        sendResponse?.(status ? { ...status, active: active && status.active,
          forwarding: forwarding && status.forwarding } : null);
      };
      statusWaiters.add(finish);
      timer = setTimeout(() => finish(null), 1_000);
      pageControl('status');
      return true;
    }
    if (['pause', 'resume'].includes(message.action) && message.version === PAGE_CONTROL_VERSION) {
      if (active) {
        if (message.action === 'pause') pause();
        else {
          void resume().then(() => sendResponse?.({ ok: forwarding }));
          return true;
        }
      }
      sendResponse?.({ ok: active });
      return false;
    }
    if (message.action === 'refresh_identity' && message.version === PAGE_CONTROL_VERSION && active) {
      window.postMessage({
        type: PAGE_CONTROL_MESSAGE_TYPE, version: PAGE_CONTROL_VERSION, action: 'refresh_identity',
      }, pageOrigin);
      sendResponse?.({ ok: true });
      return false;
    }
    if (message.action !== 'stop') return false;
    stop();
    sendResponse?.({ ok: true });
    return false;
  });
  window.addEventListener('message', pageMessageListener);
  window.addEventListener('pagehide', stop);
  void resume();
})();
