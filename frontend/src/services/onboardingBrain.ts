import type { OnboardingAdapter } from '../../../shared/onboarding/client.mjs';
// eslint-disable-next-line import/extensions -- Runtime .mjs has a matching .d.mts declaration.
import { parseOnboardingJson } from '../../../shared/onboarding/json.mjs';
// eslint-disable-next-line import/extensions -- Runtime .mjs has a matching .d.mts declaration.
import { readOnboardingEvents } from '../../../shared/onboarding/sse.mjs';

/** Source authentication is the same-origin HttpOnly session, never the UUID. */
export function createOnboardingBrainAdapter({ journeyId, fetch: request = globalThis.fetch.bind(globalThis), onDisconnect = () => {}, onFocus = () => {} }: {
  journeyId: string; fetch?: typeof fetch; onDisconnect?: () => void; onFocus?: () => void;
}): OnboardingAdapter {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(journeyId)) {
    throw new TypeError('Invalid onboarding journey');
  }
  const controller = new AbortController();
  const headers = { 'X-Onboarding-Journey': journeyId };
  let closed = false;
  let ready: () => void;
  let refuse: () => void;
  let dropped: (() => void) | null = null;
  let deadline: ReturnType<typeof setTimeout> | null = null;
  const subscribed = new Promise<void>((resolve, reject) => {
    ready = resolve; refuse = () => reject(new Error('Onboarding subscription unavailable'));
  });
  // attach always reads this promise, but subscribe failures can occur first.
  void subscribed.catch(() => undefined);
  const close = () => {
    if (closed) return;
    closed = true; if (deadline) clearTimeout(deadline);
    controller.abort(); refuse(); dropped?.(); onDisconnect();
  };
  return {
    subscribe(receive, disconnect) {
      dropped = disconnect;
      deadline = setTimeout(close, 10_000);
      void readOnboardingEvents({ fetch: request, path: '/api/v1/onboarding/events', headers,
        signal: controller.signal, ready: () => { if (deadline) clearTimeout(deadline); ready(); }, receive,
        onFocus: (id) => { if (id === journeyId) onFocus(); } }).catch(close);
      return close;
    },
    async readSnapshot() {
      await subscribed;
      deadline = setTimeout(close, 10_000);
      try {
        const response = await request('/api/v1/onboarding/state', {
          credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
          headers: { Accept: 'application/json', ...headers },
        });
        if (!response.ok) throw new Error('Onboarding state unavailable');
        return parseOnboardingJson(await response.text());
      } finally { if (deadline) clearTimeout(deadline); }
    },
    sendCommand() {
      // Pairing retains its authenticated page-owned command adapter. No
      // discovery or read subscription authorizes a new command endpoint.
      throw new Error('Onboarding command unavailable on read subscription');
    },
    invalidate: close,
  };
}
