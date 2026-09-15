import { CAPTURE_LIMITS, utf8Bytes } from './limits.mjs';
export { CAPTURE_LIMITS, fitsUtf8, utf8Bytes } from './limits.mjs';

function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const cleanup = () => signal.removeEventListener('abort', abort);
    const timer = setTimeout(() => { cleanup(); resolve(); }, ms);
    const abort = () => { clearTimeout(timer); cleanup(); reject(signal.reason); };
    signal.addEventListener('abort', abort, { once: true });
  });
}

export class CaptureDeliveryQueue {
  constructor({ send, now = Date.now, random = Math.random,
    maxEntries = CAPTURE_LIMITS.queueEntries, maxBytes = CAPTURE_LIMITS.queueBytes,
    attemptTimeoutMs = 5_000,
  }) {
    if (typeof send !== 'function') throw new TypeError('Capture delivery sender is required');
    Object.assign(this, { send, now, random, maxEntries, maxBytes, attemptTimeoutMs });
    this.entries = [];
    this.bytes = 0;
    this.processing = null;
    this.controller = new AbortController();
  }

  enqueue(delivery) {
    this.controller.signal.throwIfAborted();
    const bytes = utf8Bytes(JSON.stringify(delivery));
    if (bytes > CAPTURE_LIMITS.envelopeBytes) {
      throw Object.assign(new Error('capture_delivery_too_large'), { code: 'capture_delivery_too_large' });
    }
    if (this.entries.length >= this.maxEntries || this.bytes + bytes > this.maxBytes) {
      throw Object.assign(new Error('delivery_queue_full'), { code: 'delivery_queue_full' });
    }
    const result = new Promise((resolve, reject) => {
      this.entries.push({ delivery: structuredClone(delivery), bytes, resolve, reject });
    });
    this.bytes += bytes;
    this.#start();
    return result;
  }

  #start() {
    if (this.processing !== null || this.controller.signal.aborted) return;
    this.processing = this.#drain().finally(() => {
      this.processing = null;
      if (this.entries.length > 0) this.#start();
    });
  }

  async #drain() {
    while (this.entries.length > 0 && !this.controller.signal.aborted) {
      const item = this.entries[0];
      try { item.resolve(await this.#deliver(item.delivery, this.controller.signal)); }
      catch (error) { item.reject(error); }
      finally {
        if (this.entries[0] === item) {
          this.entries.shift();
          this.bytes -= item.bytes;
        }
      }
    }
  }

  #sendAttempt(delivery, signal, timeoutMs) {
    return new Promise((resolve, reject) => {
      signal.throwIfAborted();
      const controller = new AbortController();
      let settled = false;
      const finish = (error, response) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal.removeEventListener('abort', abort);
        if (error) { controller.abort(error); reject(error); }
        else resolve(response);
      };
      const abort = () => finish(signal.reason);
      const timer = setTimeout(() => finish(Object.assign(new Error('runtime_delivery_timeout'), {
        code: 'runtime_delivery_timeout',
      })), timeoutMs);
      signal.addEventListener('abort', abort, { once: true });
      Promise.resolve().then(() => this.send(delivery, controller.signal)).then(
        (response) => finish(null, response), (error) => finish(error),
      );
    });
  }

  async #deliver(delivery, signal) {
    let attempt = 0;
    while (this.now() - delivery.created_at_ms < CAPTURE_LIMITS.retryWindowMs) {
      signal.throwIfAborted();
      let response;
      try {
        response = await this.#sendAttempt(delivery, signal, Math.min(this.attemptTimeoutMs,
          CAPTURE_LIMITS.retryWindowMs - (this.now() - delivery.created_at_ms)));
      } catch (_error) { response = { ok: false, retryable: true }; }
      signal.throwIfAborted();
      if (response?.ok === true) return response;
      if (response?.retryable === false) {
        throw Object.assign(new Error(response?.code ?? 'delivery_rejected'), {
          code: response?.code ?? 'delivery_rejected',
        });
      }
      const remaining = CAPTURE_LIMITS.retryWindowMs - (this.now() - delivery.created_at_ms);
      if (remaining <= 0) break;
      const ceiling = Math.min(5_000, 250 * (2 ** attempt));
      attempt += 1;
      await delay(Math.min(remaining, Math.floor(this.random() * ceiling)), signal);
    }
    throw Object.assign(new Error('delivery_expired'), { code: 'delivery_expired' });
  }

  close(reason = 'capture_stopped') {
    if (!this.controller.signal.aborted) {
      this.controller.abort(Object.assign(new Error(reason), { code: reason }));
    }
    for (const item of this.entries) item.reject(this.controller.signal.reason);
    this.entries.length = 0;
    this.bytes = 0;
  }
}
