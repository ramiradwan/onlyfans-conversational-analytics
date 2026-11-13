// Setup codes continue only in the desktop app. An update removes the receive
// state that an earlier version could have kept: its database, alarm and hand-off.
export const RETIRED_SETUP_TRANSFER_DATABASE = 'ofca-setup-transfer-v1';
export const RETIRED_SETUP_TRANSFER_ALARM = 'onboarding-setup-transfer-expiry-v1';
const RETIRED_CODE_ENTRY_KEY = 'onboarding_code_entry_v1';

export function retireSetupTransfer({ chromeApi, indexedDb = globalThis.indexedDB }) {
  chromeApi.runtime.onInstalled?.addListener(({ reason } = {}) => {
    if (reason !== 'update') return;
    void Promise.resolve(chromeApi.alarms?.clear?.(RETIRED_SETUP_TRANSFER_ALARM)).catch(() => undefined);
    void Promise.resolve(chromeApi.storage?.session?.remove?.(RETIRED_CODE_ENTRY_KEY)).catch(() => undefined);
    try { indexedDb?.deleteDatabase(RETIRED_SETUP_TRANSFER_DATABASE); } catch { /* nothing to remove */ }
  });
}
