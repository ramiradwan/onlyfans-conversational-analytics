export type RecoveryClass = 'transport_interrupted' | 'operation_pending' | 'outcome_unknown'
  | 'browser_disconnected' | 'account_mismatch' | 'authentication_required'
  | 'permission_required' | 'review_required' | 'collection_paused'
  | 'version_incompatible' | 'authorization_refused' | 'cancelled'
  | 'owner_unavailable' | 'invariant_failed' | 'insufficient_evidence';
type RecoveryMap = Readonly<Record<string, RecoveryClass | null>>;
export const desktopRecovery: RecoveryMap;
export const passkeyRecovery: RecoveryMap;
export const browserRecovery: RecoveryMap;
export const browserReadRecovery: RecoveryMap;
export const operationRecovery: RecoveryMap;
export const authenticationRecovery: RecoveryMap;
export const decisionRecovery: RecoveryMap;
export const historyOwnerRecovery: RecoveryMap;
export const activationRecovery: RecoveryMap;
export const protocolRecovery: RecoveryMap;
export const captureRecovery: RecoveryMap;
export const historyRequestRecovery: RecoveryMap;
export const captureReasonRecovery: RecoveryMap;
export const catchupReasonRecovery: RecoveryMap;
export const coverageRecovery: RecoveryMap;
export const firstResultRecovery: RecoveryMap;
export const findingsRecovery: RecoveryMap;
export function recoveryClass(mapping: RecoveryMap, code: unknown): RecoveryClass | null;
