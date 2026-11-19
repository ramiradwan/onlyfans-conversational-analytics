export type SignInProvider = 'google' | 'microsoft-personal' | 'telegram' | 'tiktok' | 'default';
export interface AnalysisReadiness {
  schema: 'ofca-analysis-readiness/v1';
  commercial_authority: 'required' | 'active' | 'unavailable';
  analysis_admission: 'blocked' | 'admitted';
}
export type DesktopAnalysisReadiness =
  | { availability: 'current'; creator_account_id: string; value: AnalysisReadiness }
  | { availability: 'unknown'; creator_account_id: string | null; value: null }
  | { availability: 'unauthorized'; creator_account_id: null; value: null };
export interface DesktopAuthentication {
  operation_id: string | null;
  browser_operation_id: string | null;
  state: 'selection-required' | 'reauthentication-required' | 'replacement-required' | 'replacement-pending' | 'selected' | 'admitted' | 'retired' | 'expired' | 'unavailable';
  choices: Record<SignInProvider, boolean>;
  actions: { select: boolean; cancel: boolean; open_browser: boolean; switch_account: boolean };
  authentication_attempt: { authentication_attempt_id: string; provider: SignInProvider; mode: 'sign-in' | 'switch-account' } | null;
}
export type DesktopDecision = { operation_id: string; expires_at: string } & (
  { action: 'connect-account'; creator_id: string; computer_name: string }
  | { action: 'activate-complimentary'; offer: {
    offer_version_id: string; description: string; funding: 'complimentary-pilot'; pilot_credit_cost: number;
    capability: 'analysis-run'; licensed_major_version: number; compatible_artifact_family: string;
    update_rights: boolean; fallback_major_versions: number[];
  } });
export interface DesktopSnapshot {
  journey_id: string | null;
  generation: string;
  revision: number;
  facts: Record<string, string>;
  local_ready: boolean;
  full_ready: boolean;
  analysis_readiness: DesktopAnalysisReadiness;
  next_task: string;
  allowed_actions: string[];
  pending_operations: Array<{ operation_id: string; action: string; state: string; reason: string }>;
  decision?: DesktopDecision | null;
  decision_status?: { operation_id: string; state: 'awaiting-authentication' | 'confirmation-required' | 'accepted' | 'unavailable' } | null;
  authentication?: DesktopAuthentication | null;
  continuation_available?: boolean;
  authority_recovery?: 'available' | 'preparing' | 'sign-in-required' | 'checking' | 'restoring' | 'blocked';
  authority_recovery_browser?: 'ready' | 'launch-claimed' | 'handed-off' | 'unconfirmed';
  creator_label?: { creator_id: string; display_name: string | null; username: string | null } | null;
}
export interface DesktopState {
  snapshot: DesktopSnapshot | null;
  pending: string | null;
  pendingAction?: string;
  connected: boolean;
  error: string | null;
  deliberate?: boolean;
  browserLaunchPending?: boolean;
  recoverySignInPending?: boolean;
}
export class DesktopSetupClient {
  constructor(options: { generation: string; receive(state: DesktopState): void;
    openBrowser?: (client: DesktopSetupClient, operationId: string, invoke?: (code: string) => Promise<unknown>, current?: () => boolean) => Promise<boolean>;
    request?: typeof fetch; storage?: Storage; operatorCsrf?: () => string | null;
    focusWindow?: () => Promise<unknown>; now?: () => number;
    refreshPresentation?: (generation: string) => Promise<string> });
  session: { stage: 'setup' | 'runtime' } | null;
  json(path: string, body?: unknown): Promise<unknown>;
  browserTaskPrepared(task: { operation_id: string; journey_id: string; expires_at: number }): void;
  connect(): Promise<void>;
  wake(): void;
  act(action: string, options?: { provider?: SignInProvider; replace_ceremony?: true; recover_authority?: true }): Promise<void>;
  reconcile(selectedOperation?: string): Promise<void>;
  launchBrowser(explicit?: boolean, selectedOperation?: string): Promise<void>;
  launchProtectedBrowser(): Promise<void>;
  stop(): void;
}
