import { parseOnboardingJson } from './json.mjs';

/** One authenticated local subscription. Reconnect policy belongs to its owner. */
export async function readOnboardingEvents({ fetch: request, path, headers = {}, signal, ready, receive, onFocus, idleTimeoutMs = 45_000 }) {
  if (!['/api/v1/provisioning/events', '/api/v1/onboarding/events'].includes(path)) {
    throw new TypeError('Unregistered local event route');
  }
  const response = await request(path, {
    credentials: 'same-origin', redirect: 'error', cache: 'no-store', signal,
    headers: { Accept: 'text/event-stream', ...headers },
  });
  if (!response.ok || !response.body
    || response.headers.get('content-type')?.split(';')[0].trim() !== 'text/event-stream'
    || !response.headers.get('X-Onboarding-Capabilities')?.split(',').map((part) => part.trim()).includes('local-onboarding.v1')) {
    throw new Error('Onboarding stream unavailable');
  }
  const reader = response.body.getReader();
  let expired = false;
  let idle;
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  const arm = () => {
    clearTimeout(idle);
    idle = setTimeout(() => { expired = true; cancel(); }, idleTimeoutMs);
  };
  signal?.addEventListener('abort', cancel, { once: true });
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let pending = '';
  let event = '';
  let data = '';
  let subscribed = false;
  const line = (value) => {
    if (!value) {
      if (data && event === 'onboarding') {
        receive(parseOnboardingJson(data));
        if (!subscribed) { subscribed = true; ready(); }
      }
      if (data && event === 'workspace-focus') {
        const value = parseOnboardingJson(data);
        if (!value || typeof value !== 'object' || Object.keys(value).length !== 1
          || typeof value.journey_id !== 'string'
          || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(value.journey_id)) {
          throw new Error('Invalid workspace focus event');
        }
        onFocus?.(value.journey_id);
      }
      data = ''; event = ''; return;
    }
    if (value.startsWith(':')) return;
    const separator = value.indexOf(':');
    const name = separator < 0 ? value : value.slice(0, separator);
    const field = separator < 0 ? '' : value.slice(separator + 1).replace(/^ /u, '');
    if (name === 'event') event = field;
    else if (name === 'data') {
      data += (data ? '\n' : '') + field;
      if (data.length > 4096) throw new Error('Onboarding event too large');
    }
  };
  try {
    arm();
    // HTTP headers can precede server generator subscription. The first frame
    // proves it is listening; buffer that frame before requesting a snapshot.
    while (!signal?.aborted) {
      const chunk = await reader.read();
      if (chunk.done) break;
      arm();
      pending += decoder.decode(chunk.value, { stream: true });
      let end;
      while ((end = pending.indexOf('\n')) >= 0) {
        line(pending.slice(0, end).replace(/\r$/u, ''));
        pending = pending.slice(end + 1);
      }
      if (pending.length > 8192) throw new Error('Onboarding line too large');
    }
    if (!signal?.aborted) throw new Error(expired ? 'Onboarding stream timed out' : 'Onboarding stream disconnected');
  } finally {
    clearTimeout(idle);
    signal?.removeEventListener('abort', cancel);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
