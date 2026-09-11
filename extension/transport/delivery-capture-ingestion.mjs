import { rawIngestChange } from '../protocol/validation.mjs';
import {
  captureIsEnabled,
  mapPlatformObservation,
} from './capture-ingestion.mjs';
import { DeliveryAcceptance } from './delivery-acceptance-core.mjs';

function placeholderChatForMessage(change) {
  return {
    type: 'chat.upsert',
    chat: {
      chat_id: change.message.chat_id,
      record_kind: 'placeholder',
      platform_user_id: null,
      display_name: null,
      updated_at: null,
    },
  };
}

export class DeliveryCaptureIngestionService {
  constructor({ runtime, diagnostics }) {
    this.runtime = runtime;
    this.diagnostics = diagnostics;
    this.acceptances = new WeakMap();
  }

  rejectBridgeMessage() {
    this.diagnostics.record('invalid_bridge_message');
    return { ok: false, code: 'invalid_bridge_message', retryable: false };
  }

  async ingest(observation, { delivery, transport: admittedTransport = null, guard = {} } = {}) {
    const mapped = mapPlatformObservation(observation);
    if (!mapped.ok) {
      this.diagnostics.record(mapped.reason, mapped.eventType);
      return { ok: false, code: mapped.reason, retryable: false };
    }
    if (!delivery) return { ok: false, code: 'invalid_bridge_message', retryable: false };
    try {
      guard.signal?.throwIfAborted();
      guard.assertCurrent?.();
      const transport = admittedTransport ?? await this.runtime.wake();
      guard.assertCurrent?.();
      if (!captureIsEnabled(this.runtime.configuration?.activeDocument, mapped.resource, mapped.sourcePath)) {
        this.diagnostics.record('capture_disabled', mapped.eventType);
        return { ok: false, code: 'capture_disabled', retryable: false };
      }
      let acceptance = this.acceptances.get(transport);
      if (!acceptance) {
        acceptance = new DeliveryAcceptance({ transport, outbox: transport.outbox });
        this.acceptances.set(transport, acceptance);
      }
      let parentChange = null;
      if (mapped.change.type === 'message.upsert') {
        parentChange = placeholderChatForMessage(mapped.change);
        rawIngestChange(parentChange, '$.parent_change');
      }
      const commitGuard = { ...guard, assertCurrent: () => {
        guard.assertCurrent?.();
        if (!captureIsEnabled(this.runtime.configuration?.activeDocument, mapped.resource, mapped.sourcePath)) {
          throw Object.assign(new Error('capture_disabled'), { code: 'capture_disabled', retryable: false });
        }
      } };
      const result = await acceptance.accept({ delivery, change: mapped.change, parentChange, guard: commitGuard });
      return { ...result, event_type: mapped.eventType };
    } catch (error) {
      const permanent = ['delivery_id_conflict', 'delivery_expired', 'capture_disabled',
        'stale_capture_context', 'account_mismatch', 'identity_required'];
      const code = permanent.includes(error?.code) ? error.code : 'enqueue_failed';
      this.diagnostics.record(code, mapped.eventType);
      return { ok: false, code, retryable: code === 'enqueue_failed' };
    }
  }
}
