import { exact, isTask } from '../../shared/browser-tasks/wire.mjs';

export async function openSetupCode(client, invoke = code =>
  globalThis.__TAURI__.core.invoke('desktop_setup_code', { entryCode: code })) {
  if (!client.connected || client.session?.stage !== 'setup' || client.pending) return false;
  const result = await client.json('/api/v1/desktop/setup-transfer/entry', {});
  if (!exact(result, ['entry_code']) || !/^[A-Za-z0-9_-]{43}$/u.test(result.entry_code)) throw Error('browser_unavailable');
  if (!client.connected) return false;
  await invoke(result.entry_code);
  return true;
}

/** Only a fresh single-use code crosses the purpose-specific native command. */
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
  if (client.session?.stage === 'runtime' && matches[0].purpose === 'browser.authentication') {
    const result = await client.json('/api/v1/desktop/browser-tasks/' + matches[0].task_id + '/dispatch', {});
    if (!current()) return false;
    if (!exact(result, ['state']) || !['dispatched', 'unreachable', 'unconfirmed'].includes(result.state)) throw Error('browser_unavailable');
    if (result.state === 'dispatched') return true;
    if (result.state !== 'unreachable') throw Error('browser_unavailable');
  }
  const result = await client.json('/api/v1/desktop/browser-tasks/' + matches[0].task_id + '/entry', {});
  if (!current()) return false;
  if (!exact(result, ['entry_code']) || !/^[A-Za-z0-9_-]{43}$/u.test(result.entry_code)) throw Error('browser_unavailable');
  await invoke(result.entry_code);
  return true;
}
