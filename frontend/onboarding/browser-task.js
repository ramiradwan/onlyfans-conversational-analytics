import { exact, isTask, UUID } from '../../shared/browser-tasks/wire.mjs';

const browserActions = { 'browser-access': 'browser.access', 'finish-setup': 'browser.pair' };
const authenticationActions = ['account.connect', 'auth.select', 'auth.switch'];
const recoverableBrowserReasons = ['chrome_unavailable', 'browser_unavailable', 'extension_unavailable', 'profile_mismatch',
  'account_mismatch', 'browser_needs_account', 'browser_needs_consent', 'browser_needs_permission'];

/** Select only the operation that owns the visible setup task. Never guess from pending order. */
export function browserRecoveryTarget(snapshot, reason = null) {
  if (!snapshot || snapshot.decision || snapshot.decision_status?.state === 'accepted') return null;
  const pending = snapshot.pending_operations;
  const signIn = snapshot.authentication;
  const authentication = snapshot.next_task === 'account-connection';
  if (authentication && (!signIn?.actions.open_browser
    || !UUID.test(signIn.browser_operation_id ?? '')
    || !(signIn.state === 'admitted' && !reason || recoverableBrowserReasons.includes(reason)))) return null;
  if (!authentication && (!browserActions[snapshot.next_task]
    || reason && !recoverableBrowserReasons.includes(reason))) return null;
  const matches = pending.filter(item => authentication
    ? item.operation_id === signIn.browser_operation_id && authenticationActions.includes(item.action)
    : item.action === browserActions[snapshot.next_task]);
  if (matches.length !== 1) return null;
  const operation = matches[0];
  if (!UUID.test(operation.operation_id) || !['accepted', 'waiting', 'outcome_unknown'].includes(operation.state)
    || operation.reason !== 'none' && !recoverableBrowserReasons.includes(operation.reason)
    || operation.state === 'outcome_unknown' && !recoverableBrowserReasons.includes(reason)) return null;
  return operation.operation_id;
}

export async function openSetupCode(client, invoke = code =>
  globalThis.__TAURI__.core.invoke('desktop_setup_code', { entryCode: code })) {
  if (!client.connected || client.session?.stage !== 'setup' || client.pending) return false;
  const result = await client.json('/api/v1/desktop/setup-transfer/entry', {});
  if (!exact(result, ['entry_code']) || !/^[A-Za-z0-9_-]{43}$/u.test(result.entry_code)) throw Error('browser_unavailable');
  if (!client.connected) return false;
  await invoke(result.entry_code);
  return true;
}

/** Brain chooses the route: the paired session, the live helper, then one Chrome tab with a fresh code. */
export async function openBrowserTask(client, operationId, invoke = (code) =>
  globalThis.__TAURI__.core.invoke('desktop_browser_task', { entryCode: code }), current = () => client.connected !== false) {
  if (!current()) return false;
  const value = await client.json('/api/v1/desktop/browser-tasks');
  if (!current()) return false;
  if (!exact(value, ['tasks']) || !Array.isArray(value.tasks) || value.tasks.length > 128 || !value.tasks.every(isTask)) throw Error('browser_unavailable');
  const matches = value.tasks.filter(task => task.operation_id === operationId);
  if (matches.length === 0) return false; // The accepted operation is still preparing its task.
  if (matches.length !== 1) throw Error('browser_unavailable');
  client.browserTaskPrepared?.(matches[0]);
  const result = await client.json('/api/v1/desktop/browser-tasks/' + matches[0].task_id + '/open', {});
  if (!current()) return false;
  if (exact(result, ['state']) && ['dispatched', 'focused'].includes(result.state)) return true;
  if (exact(result, ['state']) && result.state === 'unavailable') return false;
  if (!exact(result, ['state', 'entry_code']) || result.state !== 'entry' || !/^[A-Za-z0-9_-]{43}$/u.test(result.entry_code)) {
    throw Error('browser_unavailable');
  }
  await invoke(result.entry_code);
  return true;
}

/** One explicit protected launch; no task reissue, helper session or automatic retry. */
export async function openProtectedBrowser(client, invoke = code =>
  globalThis.__TAURI__.core.invoke('desktop_browser_task', { entryCode: code })) {
  const snapshot = client.value, auth = snapshot?.authentication;
  const attempt = auth?.authentication_attempt?.authentication_attempt_id;
  if (!client.connected || snapshot?.authority_recovery !== 'sign-in-required'
    || auth?.state !== 'selected' || !auth.actions.open_browser || !UUID.test(auth.operation_id)
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(attempt ?? '')) throw Error('browser_unavailable');
  const value = await client.json('/api/v1/desktop/setup/protected-browser/open', {
    parent_operation_id: auth.operation_id, attempt_id: attempt,
    expected: { generation: snapshot.generation, revision: snapshot.revision, scope_digest: snapshot.scope_digest },
  });
  if (!exact(value, ['entry_code']) || !/^[A-Za-z0-9_-]{43}$/u.test(value.entry_code)) throw Error('browser_unavailable');
  // The accepted claim may already be visible in a newer snapshot. Only
  // retain this one response if the same workspace and child still own it.
  const current = client.value;
  if (!client.connected || current?.generation !== snapshot.generation
    || current.journey_id !== snapshot.journey_id || current.authority_recovery !== 'sign-in-required'
    || current.authentication?.state !== 'selected' || current.authentication?.operation_id !== auth.operation_id
    || current.authentication?.authentication_attempt?.authentication_attempt_id !== attempt
    || !['ready', 'launch-claimed', undefined].includes(current.authority_recovery_browser)) return false;
  await invoke(value.entry_code);
  return true;
}
