import { assertLocalServiceUrl } from './local-service-endpoints.mjs';

function failure(code) {
  return Object.assign(new Error(code), { code });
}

function abortable(promise, signal) {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener('abort', abort); reject(signal.reason); };
    signal.addEventListener('abort', abort, { once: true });
    Promise.resolve(promise).then(resolve, reject).finally(() => {
      signal.removeEventListener('abort', abort);
    });
  });
}

async function boundedBody(response, maxBytes, signal) {
  const declared = response.headers?.get?.('content-length');
  if (declared !== null && declared !== undefined && Number(declared) > maxBytes) {
    void response.body?.cancel?.().catch(() => undefined);
    throw failure('local_service_response_too_large');
  }
  const reader = response.body?.getReader?.();
  if (!reader) {
    const bytes = new Uint8Array(await abortable(response.arrayBuffer(), signal));
    if (bytes.byteLength > maxBytes) throw failure('local_service_response_too_large');
    return bytes;
  }
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await abortable(reader.read(), signal);
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) throw failure('local_service_response_too_large');
      chunks.push(value);
    }
  } catch (error) {
    void reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock?.();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

export function createSecureLocalFetch({
  fetchImpl = globalThis.fetch,
  timeoutMs = 5_000,
  maxResponseBytes = 256 * 1024,
  maxRequestBytes = 16 * 1024,
} = {}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetch is unavailable');
  for (const limit of [timeoutMs, maxResponseBytes, maxRequestBytes]) {
    if (!Number.isSafeInteger(limit) || limit <= 0) throw new TypeError('Invalid local service limit');
  }
  return async function secureLocalFetch(input, init = {}) {
    const url = assertLocalServiceUrl(input instanceof URL ? input.href : String(input));
    if (init.body !== undefined && init.body !== null && (
      typeof init.body !== 'string' || new TextEncoder().encode(init.body).byteLength > maxRequestBytes
    )) throw failure('local_service_request_too_large');
    const controller = new AbortController();
    const upstreamSignal = init.signal ?? null;
    const abort = () => controller.abort(upstreamSignal.reason);
    upstreamSignal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(() => controller.abort(failure('local_service_timeout')), timeoutMs);
    try {
      upstreamSignal?.throwIfAborted();
      const response = await abortable(fetchImpl(url.href, {
        ...init,
        signal: controller.signal,
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
        referrerPolicy: 'no-referrer',
      }), controller.signal);
      controller.signal.throwIfAborted();
      if (response.redirected || (response.url && assertLocalServiceUrl(response.url).href !== url.href)) {
        throw failure('invalid_local_service_endpoint');
      }
      if (response.status === 304 || response.body === null) return response;
      const bytes = await boundedBody(response, maxResponseBytes, controller.signal);
      controller.signal.throwIfAborted();
      return new Response(bytes, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    } catch (error) {
      if (controller.signal.aborted) throw controller.signal.reason;
      if (error?.code) throw error;
      throw failure('local_service_network_error');
    } finally {
      clearTimeout(timer);
      upstreamSignal?.removeEventListener('abort', abort);
    }
  };
}
