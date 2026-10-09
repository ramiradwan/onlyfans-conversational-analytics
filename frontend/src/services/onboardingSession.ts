
import type { RuntimeLike } from './extensionPort';
import { createOnboardingBrainAdapter } from './onboardingBrain';
import { createOnboardingExtensionAdapter } from './onboardingExtension';
// eslint-disable-next-line import/extensions -- Runtime .mjs has a matching .d.mts declaration.
import { createOnboardingClient, type OnboardingAdapter, type OnboardingClient, type OnboardingOwner } from '../../../shared/onboarding/client.mjs';
import { validateOnboardingMessage } from '../protocol/onboarding';

const RETRIES = [200, 500, 1000];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
let activeClient: OnboardingClient | null = null;
const listeners = new Set<() => void>();
const announce = () => listeners.forEach((listener) => listener());

export function currentOnboardingClient(): OnboardingClient | null { return activeClient; }
export function onboardingView() { return activeClient?.getState() ?? null; }
export function subscribeOnboarding(listener: () => void) {
  listeners.add(listener); return () => { listeners.delete(listener); };
}

export function journeyFromHash(hash: string): string | null {
  const value = hash.startsWith('#journey=') ? hash.slice(9) : '';
  return UUID.test(value) ? value : null;
}

/** Page-lifetime source connections; finite reconnects, then explicit wake recovery. */
export function startOnboardingSession({ journeyId, extensionId,
  resolveRuntime = () => (globalThis as { chrome?: { runtime?: RuntimeLike } }).chrome?.runtime,
  fetch: request = globalThis.fetch.bind(globalThis), window: page = globalThis.window,
}: { journeyId: string; extensionId: string; resolveRuntime?: () => RuntimeLike | undefined;
  fetch?: typeof fetch; window?: Window }): { client: OnboardingClient; stop(): void } {
  if (!UUID.test(journeyId)) throw new TypeError('Invalid onboarding journey');
  const client = createOnboardingClient({ journeyId, validate: validateOnboardingMessage });
  activeClient = client;
  const unsubscribe = client.subscribe(announce);
  announce();
  let stopped = false;
  let wakeAt = 0;
  const attempts: Record<OnboardingOwner, number> = { brain: 0, extension: 0 };
  const timers = new Map<OnboardingOwner, ReturnType<typeof setTimeout>>();
  const attached = new Set<OnboardingOwner>();
  let extensionAdapter: OnboardingAdapter | null = null;
  const reconnect = (source: OnboardingOwner) => {
    attached.delete(source);
    if (source === 'extension') extensionAdapter = null;
    if (stopped || timers.has(source) || attempts[source] >= RETRIES.length) return;
    const delay = RETRIES[attempts[source]++];
    timers.set(source, setTimeout(() => { timers.delete(source); connect(source); }, delay));
  };
  const connect = (source: OnboardingOwner) => {
    if (stopped || attached.has(source)) return;
    try {
      const runtime = resolveRuntime();
      // Installation is discovered on a browser wake, never on a status timer.
      if (source === 'extension' && !runtime) return;
      const adapter = source === 'brain'
        ? createOnboardingBrainAdapter({ journeyId, fetch: request, onDisconnect: () => reconnect(source),
          onFocus: () => { try { extensionAdapter?.focusWorkspace?.(); } catch { /* No focus claim without confirmation. */ } } })
        : createOnboardingExtensionAdapter({ runtime: runtime!, extensionId, onDisconnect: () => reconnect(source) });
      if (source === 'extension') extensionAdapter = adapter;
      attached.add(source);
      client.attach(source, adapter);
    } catch { attached.delete(source); reconnect(source); }
  };
  const wake = () => {
    if (page.document.hidden || Date.now() - wakeAt < 1000) return;
    wakeAt = Date.now();
    for (const source of ['brain', 'extension'] as const) {
      if (!attached.has(source) && !timers.has(source)) { attempts[source] = 0; connect(source); }
    }
  };
  page.addEventListener('focus', wake);
  page.addEventListener('pageshow', wake);
  page.document.addEventListener('visibilitychange', wake);
  connect('brain'); connect('extension');
  return { client, stop() {
    stopped = true;
    page.removeEventListener('focus', wake);
    page.removeEventListener('pageshow', wake);
    page.document.removeEventListener('visibilitychange', wake);
    for (const timer of timers.values()) clearTimeout(timer);
    timers.clear();
    client.disconnect('brain'); client.disconnect('extension');
    unsubscribe();
    if (activeClient === client) { activeClient = null; announce(); }
  } };
}
