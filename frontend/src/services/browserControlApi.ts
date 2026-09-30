import { z } from 'zod';

/**
 * Ask the creator's open extension sessions to pause or resume capture (ADR 0045).
 *
 * The response only says whether an extension session took the request. The
 * extension applies it through its own consent controller, with its usual
 * Legal checks, and the resulting state arrives in `agent.state.browser`.
 */
export type CaptureAction = 'pause' | 'resume';
export type CaptureDelivery = 'delivered' | 'unreachable';

export interface BrowserControlApi {
  setCapture(action: CaptureAction, signal?: AbortSignal): Promise<CaptureDelivery>;
}

export class BrowserControlApiError extends Error {
  constructor(readonly code: 'csrf' | 'request' | 'response' | 'timeout' | 'cancelled') {
    super('The browser extension request could not be completed.');
    this.name = 'BrowserControlApiError';
  }
}

const ENDPOINT = '/api/v1/companion/browser/capture';
const REQUEST_DEADLINE_MS = 10_000;
const deliveredSchema = z.strictObject({ delivered: z.number().int().positive().max(16) });
const unreachableSchema = z.strictObject({ detail: z.literal('browser_unreachable') });

export function createBrowserControlApi({
  fetch: request = (...args: Parameters<typeof fetch>) => globalThis.fetch(...args),
  getCsrfToken = () => document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || null,
}: { fetch?: typeof fetch; getCsrfToken?: () => string | null } = {}): BrowserControlApi {
  return {
    async setCapture(action, signal) {
      const csrf = getCsrfToken();
      if (!csrf) throw new BrowserControlApiError('csrf');
      const controller = new AbortController();
      const cancel = () => controller.abort();
      signal?.addEventListener('abort', cancel, { once: true });
      if (signal?.aborted) controller.abort();
      let timedOut = false;
      const timer = setTimeout(() => { timedOut = true; controller.abort(); }, REQUEST_DEADLINE_MS);
      try {
        const response = await request(ENDPOINT, {
          method: 'POST',
          credentials: 'same-origin',
          redirect: 'error',
          cache: 'no-store',
          headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify({ action }),
          signal: controller.signal,
        });
        const body = await response.json().catch(() => null) as unknown;
        if (response.status === 202 && deliveredSchema.safeParse(body).success) return 'delivered';
        if (response.status === 409 && unreachableSchema.safeParse(body).success) return 'unreachable';
        throw new BrowserControlApiError(response.ok ? 'response' : 'request');
      } catch (error) {
        if (timedOut) throw new BrowserControlApiError('timeout');
        if (signal?.aborted) throw new BrowserControlApiError('cancelled');
        if (error instanceof BrowserControlApiError) throw error;
        throw new BrowserControlApiError('response');
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener('abort', cancel);
      }
    },
  };
}

export const browserControlApi = createBrowserControlApi();
