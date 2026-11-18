import type { BrowserSurfacePayload } from '../protocol';
import { BrowserControlApiError, type BrowserControlApi, type CaptureAction } from './browserControlApi';

const RESPONSE_DEADLINE_MS = 10_000;

type CaptureStatus = 'waiting' | 'unknown' | 'unreachable' | 'refused';
interface CaptureRequest {
  action: CaptureAction;
  status: CaptureStatus;
  before: BrowserSurfacePayload;
}

/** A presentation guard, not another capture owner. Only the browser's
 * pushed facts establish the effect; no poll, replay or new permission. */
export function createCaptureOperation(api: BrowserControlApi) {
  let pending: CaptureRequest | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let epoch = 0;
  const listeners = new Set<() => void>();
  const publish = (value: CaptureRequest | null) => {
    pending = value;
    for (const listener of listeners) listener();
  };
  const retire = () => {
    epoch++;
    clearTimeout(timer);
    publish(null);
  };
  return {
    getState: () => pending,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    observe(browser: BrowserSurfacePayload | null) {
      if (!pending || !browser) return;
      const { action, before, status } = pending;
      if ((action === 'pause' && browser.capture === 'paused')
        || (action === 'resume' && browser.capture === 'active')) {
        retire();
      } else if (status === 'unreachable' && browser.reported_at !== before.reported_at) {
        // Delivery was refused because no browser accepted it; a fresh
        // owner report can make the original deliberate gesture available.
        retire();
      } else if (status === 'refused' && (browser.capture !== before.capture
        || browser.site_access !== before.site_access || browser.legal_review_required !== before.legal_review_required)) {
        retire();
      }
    },
    async submit(action: CaptureAction, browser: BrowserSurfacePayload) {
      if (pending || (action === 'pause' ? browser.capture !== 'active'
        : browser.capture !== 'paused' || browser.legal_review_required)) return;
      const version = ++epoch;
      const initial: CaptureRequest = { action, before: browser, status: 'waiting' };
      publish(initial);
      const current = () => epoch === version;
      const setStatus = (status: CaptureStatus) => {
        if (!current()) return;
        clearTimeout(timer);
        publish({ ...initial, status });
      };
      timer = setTimeout(() => setStatus('unknown'), RESPONSE_DEADLINE_MS);
      try {
        const result = await api.setCapture(action, new AbortController().signal);
        if (result === 'unreachable') setStatus('unreachable');
      } catch (error) {
        setStatus(error instanceof BrowserControlApiError
          && ['refused', 'not_sent'].includes(error.outcome) ? 'refused' : 'unknown');
      }
    },
  };
}

type CaptureOperation = ReturnType<typeof createCaptureOperation>;
// The dashboard and Settings must observe the same outstanding request.
// No code, receipt, or browser content is persisted; this is runtime-only.
const operations = new WeakMap<BrowserControlApi, Map<string, CaptureOperation>>();
export function captureOperation(api: BrowserControlApi, account: string | undefined): CaptureOperation {
  if (!account) return createCaptureOperation(api);
  let accounts = operations.get(api);
  if (!accounts) { accounts = new Map(); operations.set(api, accounts); }
  let operation = accounts.get(account);
  if (!operation) { operation = createCaptureOperation(api); accounts.set(account, operation); }
  return operation;
}
