import { openBrowserTask } from '../../onboarding/browser-task.js';
import { DesktopSetupClient, type DesktopState } from '../../onboarding/client.js';
import { authorityRecoveryOptions, authorityRecoveryView, type RecoveryAction } from '../../shared-setup/authority-recovery.js';
import type { BrowserSurfacePayload, FullPresentationIntent } from '../protocol';

/** Display only: current native/account-scoped readiness never admits a command. */
export function selectAnalysisReadiness(value: DesktopState, creatorAccountId?: string | null) {
  const readiness = value.snapshot?.analysis_readiness;
  if (!value.connected || !readiness || (creatorAccountId !== undefined
    && readiness.creator_account_id !== creatorAccountId)) {
    return { value: null, availability: 'unknown' as const, checking: value.snapshot === null && value.error === null };
  }
  return { value: readiness.value, availability: readiness.availability, checking: false };
}


/**
 * Select the existing capture owner, independently of commercial authority.
 * The paired extension reports current capture facts; the desktop snapshot
 * admits review. Neither a reported mode nor a UI choice grants consent.
 */
export function selectCollectionContinuation(desktop: DesktopState, creatorAccountId: string | null | undefined,
  browser: BrowserSurfacePayload | null, canManage: boolean) {
  const snapshot = desktop.snapshot;
  const current = Boolean(canManage && creatorAccountId && browser && desktop.connected && snapshot?.local_ready
    && snapshot.analysis_readiness.availability === 'current'
    && snapshot.analysis_readiness.creator_account_id === creatorAccountId);
  const reviewOperation = current ? snapshot?.pending_operations.find(operation => operation.action === 'browser.access') : undefined;
  // Never replace a different admitted operation with a new browser command.
  const localReviewPending = current && Boolean(desktop.pending && desktop.pendingAction === 'browser.access');
  const otherOperation = Boolean(snapshot?.pending_operations.some(operation => operation.action !== 'browser.access'));
  const available = current && !desktop.pending && !desktop.browserLaunchPending && !otherOperation && !reviewOperation;
  const reviewNeeded = Boolean(browser && (browser.legal_review_required || browser.site_access !== 'granted' || browser.capture === 'off'));
  const paused = browser?.capture === 'paused' && !reviewNeeded;
  const canReview = available && reviewNeeded && snapshot?.allowed_actions.includes('browser.access') === true;
  // Resume already has a separate creator-only, authenticated capture endpoint.
  // The extension, not this selector, checks the consent and legal state again.
  const canResume = available && paused;
  return { current, reviewNeeded, paused, reviewOperation, localReviewPending, canReview, canResume,
    actionable: Boolean(canReview || canResume || reviewOperation || localReviewPending) };
}

// The companion report is authenticated to its account. The native owner
// still decides whether the exact current journey may present a review.
export function selectFullPresentationIntent(desktop: DesktopState, creatorAccountId: string | null | undefined,
  browser: BrowserSurfacePayload | null): FullPresentationIntent | undefined {
  const snapshot = desktop.snapshot;
  const intent = browser?.full_intent;
  if (!creatorAccountId || !intent || browser?.legal_review_required || browser?.capture === 'off'
    || !desktop.connected || !snapshot?.local_ready || snapshot.journey_id !== intent.journey_id
    || snapshot.analysis_readiness.availability !== 'current'
    || snapshot.analysis_readiness.creator_account_id !== creatorAccountId
    || snapshot.analysis_readiness.value?.commercial_authority !== 'required'
    || !(snapshot.allowed_actions.includes('activation.review')
      || snapshot.decision?.action === 'activate-complimentary' && snapshot.allowed_actions.includes('activation.confirm'))) return undefined;
  return intent;
}

export type DesktopAction = 'activation.review' | 'activation.confirm' | 'browser.pair' | 'browser.access' | 'auth.select' | 'auth.cancel' | 'auth.switch';
export type AuthenticationProvider = 'google' | 'microsoft-personal' | 'telegram' | 'tiktok' | 'default';
export interface DesktopOnboardingApi {
  getState(): DesktopState;
  subscribe(listener: () => void): () => void;
  begin(action: DesktopAction, options?: { provider?: AuthenticationProvider }): Promise<void>;
  openBrowser(operationId: string): Promise<void>;
  reconcile(action: DesktopAction): Promise<void>;
}

const listeners = new Set<() => void>();
let client: DesktopSetupClient | null = null;
let state: DesktopState = { snapshot: null, pending: null, connected: false, error: null };

export const desktopOnboardingState = () => state;
function publish(next: DesktopState) { state = next; for (const listener of listeners) listener(); }

function connect() {
  if (client) { void client.connect(); return; }
  let generation: string | null = null;
  try { generation = sessionStorage.getItem('desktop-presentation'); } catch { /* No admitted locator. */ }
  if (!generation || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(generation)) {
    publish({ ...state, connected: false, error: 'session_unavailable' }); return;
  }
  // The locator was saved by the consumed native entry. Brain still requires
  // its exact desktop cookie/generation and the current operator for commands.
  const next = new DesktopSetupClient({ generation,
    receive: value => { if (client === next) publish(value); }, openBrowser: openBrowserTask });
  client = next;
  void client.connect();
}

export function subscribeDesktopOnboarding(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) connect();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) { client?.stop(); client = null; state = { ...state, connected: false }; }
  };
}

export async function beginDesktopAction(action: DesktopAction, options: { provider?: AuthenticationProvider } = {}) {
  if (!client || !state.connected || !state.snapshot) throw Error('session_unavailable');
  const pending = state.snapshot.pending_operations.find(value => value.action === action);
  const replacesReview = action === 'activation.review' && pending && state.snapshot.allowed_actions.includes(action);
  if (pending && !replacesReview) {
    if (options.provider !== undefined && state.snapshot.authentication?.authentication_attempt?.provider !== options.provider) throw Error('operation_pending');
    await client.launchBrowser(true, pending.operation_id);
  }
  else {
    const authentication = state.snapshot.authentication;
    const replacesCurrentAuthentication = ['auth.cancel', 'auth.switch'].includes(action)
      && [authentication?.operation_id, authentication?.browser_operation_id].includes(state.pending);
    const replacesReviewChild = replacesReview && state.snapshot.pending_operations.some(item =>
      item.operation_id === state.pending && ['activation.review', 'activation.confirm', 'auth.select', 'auth.switch', 'auth.cancel'].includes(item.action));
    if (state.pending !== null && !replacesCurrentAuthentication && !replacesReviewChild) throw Error('operation_pending');
    if (!state.snapshot.allowed_actions.includes(action)) throw Error('action_unavailable');
    await client.act(action, options);
  }
  if (state.error) throw Error(state.error);
}

export async function openDesktopBrowser(operationId: string) {
  if (!client || !state.connected || !state.snapshot) throw Error('session_unavailable');
  const authentication = state.snapshot.authentication;
  if (!authentication?.actions.open_browser || authentication.browser_operation_id !== operationId) throw Error('browser_unavailable');
  await client.launchBrowser(true, operationId);
  if (state.error) throw Error(state.error);
}

export function reconnectDesktopOnboarding() { connect(); }
export async function reconcileDesktopOnboarding(action: DesktopAction) {
  if (!client || !state.connected || !state.snapshot) throw Error('session_unavailable');
  const pending = state.snapshot.pending_operations.find(value => value.action === action);
  if (!pending) throw Error('operation_unavailable');
  await client.reconcile(pending.operation_id);
  if (state.error) throw Error(state.error);
}

export const desktopOnboardingApi: DesktopOnboardingApi = { getState: desktopOnboardingState,
  subscribe: subscribeDesktopOnboarding, begin: beginDesktopAction, openBrowser: openDesktopBrowser, reconcile: reconcileDesktopOnboarding };

/** The native access gate never requires an expired local operator session. */
export interface AuthorityRecoveryApi {
  getState(): DesktopState;
  subscribe(listener: () => void): () => void;
  act(action: RecoveryAction): Promise<void>;
}
export const authorityRecoveryApi: AuthorityRecoveryApi = {
  getState: desktopOnboardingState,
  subscribe: subscribeDesktopOnboarding,
  async act(action) {
    const current = client;
    const view = authorityRecoveryView(state);
    if (!current || !view || view.disabled || (action !== view.action && !(action === 'auth.cancel' && view.cancel))) throw Error('action_unavailable');
    try {
      if (action === 'browser.open') await current.launchProtectedBrowser();
      else await current.act(action, authorityRecoveryOptions(action));
    } catch (error) {
      if (client === current && !(error instanceof Error && error.message === 'chrome_unavailable') && current.session) {
        void current.connect();
      }
      throw error;
    }
  },
};
