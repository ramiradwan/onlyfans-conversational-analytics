import { openBrowserTask } from '../../onboarding/browser-task.js';
import { DesktopSetupClient, type DesktopState } from '../../onboarding/client.js';

/** Display only: current native/account-scoped readiness never admits a command. */
export function selectAnalysisReadiness(value: DesktopState, creatorAccountId?: string | null) {
  const readiness = value.snapshot?.analysis_readiness;
  if (!value.connected || !readiness || (creatorAccountId !== undefined
    && readiness.creator_account_id !== creatorAccountId)) {
    return { value: null, availability: 'unknown' as const, checking: value.snapshot === null && value.error === null };
  }
  return { value: readiness.value, availability: readiness.availability, checking: false };
}


export type DesktopAction = 'activation.review' | 'activation.confirm' | 'browser.pair' | 'browser.access' | 'auth.select' | 'auth.cancel' | 'auth.switch';
export type AuthenticationProvider = 'google' | 'microsoft-personal' | 'default';
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
  if (pending) {
    if (options.provider !== undefined && state.snapshot.authentication?.authentication_attempt?.provider !== options.provider) throw Error('operation_pending');
    await client.launchBrowser(true, pending.operation_id);
  }
  else {
    const authentication = state.snapshot.authentication;
    const replacesCurrentAuthentication = ['auth.cancel', 'auth.switch'].includes(action)
      && [authentication?.operation_id, authentication?.browser_operation_id].includes(state.pending);
    if (state.pending !== null && !replacesCurrentAuthentication) throw Error('operation_pending');
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
