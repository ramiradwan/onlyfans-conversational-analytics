import { CAPTURE_LIMITS } from '../capture/delivery-queue.mjs';

export const DELIVERY_RECEIPT_SCHEMA = 'ofca-delivery-receipt/v1';
export const DELIVERY_RECEIPT_RETENTION_MS = 10 * 60_000;

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  }
  return value;
}

export function assertDeliveryFresh(delivery, now = Date.now()) {
  if (!Number.isSafeInteger(delivery?.created_at_ms)
    || delivery.created_at_ms > now
    || now - delivery.created_at_ms >= CAPTURE_LIMITS.retryWindowMs) {
    throw Object.assign(new Error('delivery_expired'), { code: 'delivery_expired', retryable: false });
  }
}

export class DeliveryAcceptance {
  constructor({ transport, outbox, now = Date.now, cryptoApi = globalThis.crypto }) {
    if (typeof transport?.flushOutbox !== 'function') throw new Error('Capture transport is required');
    if (typeof outbox?.enqueueDelivery !== 'function') throw new Error('Transactional delivery outbox is required');
    Object.assign(this, { transport, outbox, now, cryptoApi });
  }

  async accept({ delivery, change, parentChange = null, guard = {} }) {
    guard.signal?.throwIfAborted();
    guard.assertCurrent?.();
    assertDeliveryFresh(delivery, this.now());
    const bytes = new TextEncoder().encode(JSON.stringify(stable({ delivery, change, parentChange })));
    const hash = await this.cryptoApi.subtle.digest('SHA-256', bytes);
    const payloadDigest = [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
    const result = await this.outbox.enqueueDelivery({
      delivery, change, parentChange, payloadDigest, now: this.now,
    }, guard);
    // Local acceptance is committed; companion availability cannot change the ACK.
    void Promise.resolve().then(() => this.transport.flushOutbox()).catch(() => undefined);
    return result;
  }
}
