import type { BrowserSurfacePayload } from '../protocol';
import { BrowserControlApiError, type BrowserControlApi, type CaptureAction, type CaptureRequestScope } from './browserControlApi';

const RESPONSE_DEADLINE_MS = 10_000;

type CaptureStatus = 'waiting' | 'unknown' | 'unreachable' | 'refused';
interface CaptureRequest {
  action: CaptureAction;
  status: CaptureStatus;
  before: BrowserSurfacePayload;
  scope?: CaptureRequestScope;
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
      const { action, before, status, scope } = pending;
      if (scope) {
        const control = browser.capture_control;
        if (!control || control.capture !== browser.capture) return;
        const receipt = control.receipt;
        if (control.owner_scope !== scope.owner_scope || control.policy_revision !== scope.policy_revision) { retire(); return; }
        if (receipt?.operation_id === scope.operation_id && receipt.action === action
          && receipt.previous_consent_epoch === scope.consent_epoch) {
          if (receipt.outcome === 'applied' && control.consent_epoch !== scope.consent_epoch
            && browser.capture === (action === 'pause' ? 'paused' : 'active')) { retire(); return; }
          if (receipt.outcome === 'refused' && control.consent_epoch === scope.consent_epoch) {
            if (status === 'refused' && (browser.site_access !== before.site_access
              || browser.legal_review_required !== before.legal_review_required)) { retire(); return; }
            clearTimeout(timer);
            if (status !== 'refused') publish({ ...pending, status: 'refused' });
            return;
          }
        }
        // A newer owner epoch fences the old request, regardless of which
        // gesture caused the change. Do not label it as our successful receipt.
        if (control.consent_epoch !== scope.consent_epoch) { retire(); return; }
        // A known pre-dispatch rejection can only be released by fresh facts.
        if (status === 'unreachable' && browser.reported_at !== before.reported_at) retire();
        else if (status === 'refused' && (browser.site_access !== before.site_access
          || browser.legal_review_required !== before.legal_review_required)) retire();
        return;
      }
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
      if (pending || (browser.capture_control_supported && browser.capture_control?.capture !== browser.capture)
        || (action === 'pause' ? browser.capture !== 'active'
        : browser.capture !== 'paused' || browser.legal_review_required)) return;
      const version = ++epoch;
      const control = browser.capture_control;
      const scope = control?.capture === browser.capture ? {
        operation_id: crypto.randomUUID(), owner_scope: control.owner_scope, consent_epoch: control.consent_epoch,
        policy_revision: control.policy_revision,
      } : undefined;
      const initial: CaptureRequest = { action, before: browser, status: 'waiting', scope };
      publish(initial);
      const current = () => epoch === version;
      const setStatus = (status: CaptureStatus) => {
        if (!current()) return;
        clearTimeout(timer);
        publish({ ...initial, status });
      };
      timer = setTimeout(() => setStatus('unknown'), RESPONSE_DEADLINE_MS);
      try {
        const signal = new AbortController().signal;
        const result = await (scope ? api.setCapture(action, signal, scope) : api.setCapture(action, signal));
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
