// Classification describes the owner's result, not a new recovery owner.
// A normal state has no recovery class. Unrecognised codes fail closed.
export const desktopRecovery = Object.freeze({
  none: null,
  status_unavailable: 'transport_interrupted',
  connection_unavailable: 'owner_unavailable',
  session_unavailable: 'invariant_failed',
  authorization_unavailable: 'authorization_refused',
  operation_unconfirmed: 'outcome_unknown',
  operation_pending: 'operation_pending',
  operation_refused: 'authorization_refused',
  operation_unavailable: 'invariant_failed',
  action_unavailable: 'authorization_refused',
  authority_unavailable: 'owner_unavailable',
  browser_unavailable: 'browser_disconnected',
  browser_recheck_required: 'browser_disconnected',
  chrome_unavailable: 'browser_disconnected',
  browser_preparing: 'operation_pending',
  extension_unavailable: 'browser_disconnected',
  profile_mismatch: 'account_mismatch',
  account_mismatch: 'account_mismatch',
  browser_needs_account: 'account_mismatch',
  browser_needs_consent: 'review_required',
  browser_needs_permission: 'permission_required',
  operation_expired: 'authentication_required',
  activation_unavailable: 'owner_unavailable',
  invalid_snapshot: 'invariant_failed',
  invalid_receipt: 'invariant_failed',
});
export const passkeyRecovery = Object.freeze({
  passkey_status_unavailable: 'owner_unavailable',
  passkey_status_network: 'transport_interrupted',
  passkey_status_session: 'authentication_required',
  passkey_status_forbidden: 'authorization_refused',
  passkey_status_invalid: 'invariant_failed',
  passkey_sign_in_required: 'authentication_required',
  passkey_start_unavailable: 'owner_unavailable',
  passkey_unconfirmed: 'outcome_unknown',
});
export const browserRecovery = Object.freeze({
  loading: 'operation_pending',
  unavailable: 'browser_disconnected',
  profile_mismatch: 'account_mismatch',
  account_mismatch: 'account_mismatch',
  auth_idle: 'authentication_required',
  auth_opening: 'operation_pending',
  auth_checking: 'operation_pending',
  auth_unconfirmed: 'outcome_unknown',
  auth_access_required: 'permission_required',
  auth_closed: 'authentication_required',
  pairing_connecting: 'operation_pending',
  pairing_checking: 'operation_pending',
  pairing_saved: 'review_required',
  pairing_unconfirmed: 'outcome_unknown',
  pairing_access_required: 'permission_required',
  task_expired: 'authentication_required',
  task_changed: 'invariant_failed',
  task_completed: null,
  service_unavailable: 'owner_unavailable',
  status_reconnecting: 'transport_interrupted',
  extension_disconnected: 'browser_disconnected',
  'needs-consent': 'review_required',
  'needs-permission': 'permission_required',
  'needs-account': 'account_mismatch',
  ready: 'operation_pending',
  paused: 'collection_paused',
  reported: null,
  invalid_response: 'invariant_failed',
});
export const browserReadRecovery = Object.freeze({
  task_changed: 'invariant_failed',
  task_expired: 'authentication_required',
  invalid_response: 'invariant_failed',
  read_timeout: 'transport_interrupted',
  owner_unavailable: 'transport_interrupted',
  request_refused: 'authorization_refused',
  cancelled: 'cancelled',
  preparation_unavailable: 'owner_unavailable',
});
export const operationRecovery = Object.freeze({
  accepted: 'operation_pending',
  waiting: 'operation_pending',
  outcome_unknown: 'outcome_unknown',
  completed: null,
  refused: 'authorization_refused',
});
export const authenticationRecovery = Object.freeze({
  'selection-required': 'authentication_required',
  'reauthentication-required': 'authentication_required',
  'replacement-required': 'authentication_required',
  'replacement-pending': 'operation_pending',
  selected: 'operation_pending',
  admitted: null,
  retired: 'cancelled',
  expired: 'authentication_required',
  unavailable: 'owner_unavailable',
});
export const decisionRecovery = Object.freeze({
  'awaiting-authentication': 'authentication_required',
  'confirmation-required': 'review_required',
  accepted: null,
  unavailable: 'owner_unavailable',
});
export const historyOwnerRecovery = Object.freeze({
  identity_required: 'account_mismatch',
  unsupported_revision: 'version_incompatible',
  history_unavailable: 'owner_unavailable',
  history_dependency_wait: 'operation_pending',
  software_update_required: 'version_incompatible',
});
export const activationRecovery = Object.freeze({
  redemption_expired: 'authorization_refused',
  redemption_invalid: 'authorization_refused',
  redemption_conflict: 'authorization_refused',
  redemption_unauthorized: 'authorization_refused',
  redemption_mismatch: 'account_mismatch',
  local_installation_authority_unavailable: 'owner_unavailable',
  installation_key_unavailable: 'owner_unavailable',
  durable_store_unavailable: 'owner_unavailable',
  hosted_origin_unavailable: 'outcome_unknown',
  hosted_unavailable: 'outcome_unknown',
});
export const protocolRecovery = Object.freeze({
  unsupported_version: 'version_incompatible',
  wrong_role: 'authorization_refused',
  pre_handshake: 'invariant_failed',
  identity_conflict: 'account_mismatch',
  validation_failed: 'invariant_failed',
  unauthorized: 'authentication_required',
  internal_error: 'invariant_failed',
});
export const captureRecovery = Object.freeze({
  waiting: 'operation_pending',
  unknown: 'outcome_unknown',
  unreachable: 'browser_disconnected',
  refused: 'authorization_refused',
});
export const historyRequestRecovery = Object.freeze({
  transport: 'transport_interrupted',
  conflict: 'outcome_unknown',
  refused: 'authorization_refused',
  unknown: 'outcome_unknown',
});
export const captureReasonRecovery = Object.freeze({
  ok: null, capture_off: 'collection_paused', consent_needed: 'review_required',
  no_onlyfans_tab: 'browser_disconnected', tab_frozen: 'browser_disconnected',
  tab_discarded: 'browser_disconnected', hook_not_armed: 'operation_pending',
  reload_required: 'browser_disconnected', page_socket_closed: 'transport_interrupted',
  account_mismatch: 'account_mismatch', storage_locked: 'owner_unavailable',
  paused: 'collection_paused',
});
export const catchupReasonRecovery = Object.freeze({
  user_paused: 'collection_paused', consent_needed: 'review_required',
  extension_offline: 'browser_disconnected', no_onlyfans_tab: 'browser_disconnected',
  onlyfans_sleeping: 'browser_disconnected', account_changed: 'account_mismatch',
  applying_settings: 'operation_pending', capture_off: 'collection_paused',
  extension_outdated: 'version_incompatible', catch_up: 'operation_pending',
  canary: 'operation_pending', awaiting_check: 'operation_pending',
  daily_cap: 'operation_pending', check_incomplete: 'operation_pending',
  not_observing: 'operation_pending',
});
export const coverageRecovery = Object.freeze({
  consent_revoked: 'collection_paused', configuration_not_applied: 'operation_pending',
  history_sync_paused: 'collection_paused', inventory_not_frozen: 'operation_pending',
  conversation_evidence_missing: 'insufficient_evidence', new_generation: 'operation_pending',
  new_conversation_discovered: 'operation_pending',
  not_observed: 'insufficient_evidence', head_not_reconciled: 'operation_pending',
});
export const firstResultRecovery = Object.freeze({
  projection_degraded: 'invariant_failed', projection_lag: 'operation_pending',
  projection_generation_failed: 'invariant_failed',
  waiting_for_messages: 'insufficient_evidence',
});
export const findingsRecovery = Object.freeze({
  analytics_projection_building: 'operation_pending',
  analytics_projection_unavailable: 'owner_unavailable',
  analytics_projection_error: 'invariant_failed',
  analytics_findings_invalid: 'invariant_failed',
  analytics_literals_invalid: 'invariant_failed',
  analytics_findings_metadata_unavailable: 'owner_unavailable',
});
export function recoveryClass(mapping, code) {
  if (code == null) return null;
  return typeof code === 'string' && Object.hasOwn(mapping, code) ? mapping[code] : 'invariant_failed';
}
