import {
  CAPTURE_DELIVERY_TYPE,
  isCaptureDelivery,
} from '../capture/envelopes.mjs';
import {
  assertCaptureContext,
  captureSenderKey,
  isCaptureContextError,
} from '../runtime/capture-context.mjs';

function observationWithoutPageEpoch(observation) {
  if (observation?.event_type === 'hook.diagnostic') return observation;
  const { page_epoch: _pageEpoch, ...mapped } = observation;
  return mapped;
}

export function createAccountBoundCaptureMessageBridge({
  ingestion,
  runtime,
  provisioningIdentityBridge,
  allowsCapture,
  currentConsent = () => null,
  ensureReady = async () => {},
  diagnostics,
  operationScope = null,
  chromeApi = globalThis.chrome,
}) {
  if (
    typeof ingestion?.ingest !== 'function'
    || typeof ingestion?.rejectBridgeMessage !== 'function'
  ) throw new Error('Capture ingestion service is required');
  if (typeof runtime?.wake !== 'function') throw new Error('Agent runtime is required');
  if (typeof provisioningIdentityBridge?.withCaptureContext !== 'function') {
    throw new Error('Provisioning identity bridge is required');
  }
  if (typeof allowsCapture !== 'function') throw new Error('Capture authorization is required');
  if (typeof diagnostics?.record !== 'function') throw new Error('Capture diagnostics are required');
  if (operationScope !== null && typeof operationScope?.run !== 'function') {
    throw new Error('Capture operation scope is invalid');
  }
  if (!chromeApi?.runtime?.onMessage?.addListener) {
    throw new Error('chrome.runtime.onMessage is unavailable');
  }

  let registered = false;

  const rejectContext = (error, eventType) => {
    const code = isCaptureContextError(error)
      ? error.code
      : ['scope_closed', 'capture_transition', 'capture_reconcile', 'capture_disabled']
          .includes(error?.code)
        ? 'capture_disabled'
        : 'enqueue_failed';
    diagnostics.record(code, eventType);
    return {
      ok: false,
      code,
      retryable: code === 'enqueue_failed',
    };
  };

  const ingestAuthorized = (delivery, sender, transport, lease = null) => (
    provisioningIdentityBridge.withCaptureContext(sender, async (context, assertDocumentCurrent = () => {}) => {
      const observation = delivery.observation;
      const assertAuthorized = () => {
        lease?.assertCurrent();
        assertDocumentCurrent();
        if (!allowsCapture()) throw Object.assign(new Error('capture_disabled'), { code: 'capture_disabled' });
        assertCaptureContext({
          sender, extensionId: chromeApi.runtime.id, context,
          consent: currentConsent(), delivery,
          configuration: runtime.configuration?.activeDocument,
          boundCreatorAccountId: transport?.creatorAccountId,
          observation,
        });
        if (runtime.transport && runtime.transport !== transport) {
          throw Object.assign(new Error('account_mismatch'), { code: 'account_mismatch' });
        }
      };
      assertAuthorized();
      const result = await ingestion.ingest(observationWithoutPageEpoch(observation), {
        delivery, transport, guard: { signal: lease?.signal, assertCurrent: assertAuthorized },
      });
      return result;
    })
  );

  const performCapture = async (delivery, sender, lease = null) => {
    const observation = delivery.observation;
    if (observation.event_type === 'hook.diagnostic') {
      return ingestion.ingest(observation, { delivery });
    }
    if (!allowsCapture()) {
      diagnostics.record('capture_disabled', observation.event_type);
      return { ok: false, code: 'capture_disabled', retryable: false };
    }
    lease?.assertCurrent();
    const transport = await runtime.wake();
    lease?.assertCurrent();
    return ingestAuthorized(delivery, sender, transport, lease);
  };

  const listener = (message, sender, sendResponse) => {
    if (![CAPTURE_DELIVERY_TYPE, 'ofca.capture.context.query'].includes(message?.type)) return false;
    try {
      captureSenderKey(sender, chromeApi.runtime.id);
    } catch (_error) {
      return false;
    }
    if (message.type === 'ofca.capture.context.query') {
      void ensureReady().then(() => sendResponse({
        ok: true, consent_epoch: currentConsent()?.consent_epoch ?? null,
      }), () => sendResponse({ ok: false }));
      return true;
    }
    if (!isCaptureDelivery(message)) {
      sendResponse(ingestion.rejectBridgeMessage());
      return false;
    }

    void (async () => {
      const observation = message.observation;
      try {
        await ensureReady();
        return operationScope === null
          ? await performCapture(message, sender)
          : await operationScope.run((lease) => performCapture(message, sender, lease));
      } catch (error) {
        return rejectContext(error, observation.event_type);
      }
    })().then(
      (response) => sendResponse(response),
      () => sendResponse({ ok: false, code: 'enqueue_failed', retryable: true }),
    );
    return true;
  };

  return Object.freeze({
    register() {
      if (registered) return;
      chromeApi.runtime.onMessage.addListener(listener);
      registered = true;
    },
    unregister() {
      if (!registered) return;
      chromeApi.runtime.onMessage.removeListener?.(listener);
      registered = false;
    },
    listener,
  });
}
