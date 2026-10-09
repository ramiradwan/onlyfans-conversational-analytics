// eslint-disable-next-line import/extensions -- Runtime .mjs has a matching .d.mts declaration.
import { parseOnboardingJson } from '../../../shared/onboarding/json.mjs';

export type ActivationReturnState = 'none' | 'waiting' | 'ready' | 'checking' | 'unconfirmed';
interface ActivationReturnReply { state: ActivationReturnState; entry_id: string | null }
export interface ActivationReturnApi {
  start(journeyId: string, isCurrent?: () => boolean): Promise<ActivationReturnState>;
  check(journeyId: string): Promise<ActivationReturnState>;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const PATH = '/api/v1/onboarding/activation-return';

export function createActivationReturnApi({
  fetch: request = (...args: Parameters<typeof fetch>) => globalThis.fetch(...args),
  csrf = () => document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || null,
  storage = () => globalThis.sessionStorage,
}: { fetch?: typeof fetch; csrf?: () => string | null; storage?: () => Pick<Storage, 'getItem' | 'setItem'> } = {}): ActivationReturnApi {
  const attempted = new Set<string>();
  const pending = new Map<string, { work: Promise<ActivationReturnState>; callers: Set<() => boolean> }>();
  const invoke = async (journey: string, entryId: string | null): Promise<ActivationReturnReply> => {
    const mutate = entryId !== null;
    if (!UUID.test(journey)) throw new Error('Invalid journey');
    const token = mutate ? csrf() : null;
    if (mutate && !token) throw new Error('Local sign-in required');
    const controller = new AbortController();
    let deadline: ReturnType<typeof setTimeout>;
    const expired = new Promise<never>((_resolve, reject) => {
      deadline = setTimeout(() => { controller.abort(); reject(new Error('Activation return timed out')); }, 10_000);
    });
    const work = (async (): Promise<ActivationReturnReply> => {
      const response = await request(PATH, {
        method: mutate ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
        redirect: 'error', signal: controller.signal,
        headers: { Accept: 'application/json', 'X-Onboarding-Journey': journey,
          ...(token ? { 'Content-Type': 'application/json', 'X-CSRF-Token': token,
            'X-Onboarding-Activation-Entry': entryId! } : {}) },
        ...(mutate ? { body: '{}' } : {}),
      });
      if (controller.signal.aborted || !response.ok || !response.body
        || response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') {
        throw new Error('Activation return unavailable');
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8', { fatal: true });
      let raw = ''; let size = 0;
      try {
        while (true) {
          const part = await reader.read();
          if (controller.signal.aborted) throw new Error('Activation return timed out');
          if (part.done) break;
          size += part.value.byteLength;
          if (size > 1024) throw new Error('Invalid activation return');
          raw += decoder.decode(part.value, { stream: true });
        }
        raw += decoder.decode();
      } finally { void reader.cancel().catch(() => undefined); reader.releaseLock(); }
      const parsed = parseOnboardingJson(raw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Invalid activation return');
      const value = parsed as Record<string, unknown>;
      if (Object.keys(value).length !== 3 || value.journey_id !== journey
        || !['none', 'waiting', 'ready', 'checking', 'unconfirmed'].includes(String(value.state))
        || !(value.entry_id === null || (typeof value.entry_id === 'string' && UUID.test(value.entry_id)))
        || (['waiting', 'ready', 'checking'].includes(String(value.state)) && value.entry_id === null)
        || (value.state === 'none' && value.entry_id !== null)
        || (entryId !== null && value.entry_id !== entryId)) {
        throw new Error('Invalid activation return');
      }
      return { state: value.state as ActivationReturnState, entry_id: value.entry_id as string | null };
    })();
    try { return await Promise.race([work, expired]); } finally { clearTimeout(deadline!); }
  };
  const read = async (journey: string): Promise<ActivationReturnReply> => {
    try { return await invoke(journey, null); } catch { return { state: 'unconfirmed', entry_id: null }; }
  };
  return {
    check: async (journey) => (await read(journey)).state,
    start(journey, isCurrent = () => true) {
      const existing = pending.get(journey);
      if (existing) { existing.callers.add(isCurrent); return existing.work; }
      const callers = new Set([isCurrent]);
      const active = () => [...callers].some((current) => current());
      const work = (async (): Promise<ActivationReturnState> => {
        const initial = await read(journey);
        if (!active()) return 'none';
        if (initial.state !== 'ready' || initial.entry_id === null) return initial.state;
        const entryId = initial.entry_id;
        const marker = `activation-return:attempted:${entryId}`;
        try {
          if (attempted.has(entryId) || storage().getItem(marker) !== null) return 'unconfirmed';
          storage().setItem(marker, '1');
          if (storage().getItem(marker) !== '1') return 'unconfirmed';
        } catch { return 'unconfirmed'; }
        // Mark before dispatch. An uncertain reply is reconciled by a read;
        // the native owner remains responsible for the exact redemption result.
        attempted.add(entryId);
        try {
          const result = await invoke(journey, entryId);
          return result.state === 'checking' ? result.state : 'unconfirmed';
        } catch {
          if (!active()) return 'unconfirmed';
          const reconciled = await read(journey);
          return reconciled.entry_id === entryId && reconciled.state === 'checking' ? 'checking' : 'unconfirmed';
        }
      })();
      const settled = work.finally(() => pending.delete(journey));
      pending.set(journey, { work: settled, callers });
      return settled;
    },
  };
}

export const activationReturnApi = createActivationReturnApi();
