const openEvent = 'dipsy-passkey-open';

function nativePasskeyGeneration() {
  try {
    return typeof globalThis.__TAURI__?.core?.invoke === 'function' ? sessionStorage.getItem('desktop-presentation') : null;
  } catch { return null; }
}

export function nativePasskeyPresentation() {
  return Boolean(nativePasskeyGeneration());
}

export async function passkeyWindow(action) {
  if (!nativePasskeyPresentation() || document.hidden && action !== 'discard_passkey_unlock') return false;
  try {
    return await globalThis.__TAURI__.core.invoke('desktop_window', { action }) === true;
  } catch { return false; }
}

export const passkeyPermissionMessage = 'In Windows Settings, go to Privacy & security, then Passkey access. Allow Dipsy, then try again.';

export function openPasskeySettings() {
  return passkeyWindow('open_passkey_settings');
}

// The native window owns the one-use Open. Events only ask the active view
// to check it. A focus change or a new status read cannot create another Open.
export function followPasskeyOpen(ready, unlock) {
  let stopped = false;
  let checked = false;
  let pending = false;
  const receive = async () => {
    if (stopped || pending || !ready()) return;
    const generation = nativePasskeyGeneration();
    if (!generation) return;
    pending = true;
    try {
      if (await passkeyWindow('claim_passkey_unlock') && !stopped && !document.hidden
        && generation === nativePasskeyGeneration() && ready()) unlock();
    } finally { pending = false; }
  };
  window.addEventListener(openEvent, receive);
  return {
    check() {
      if (checked || !ready()) return;
      checked = true;
      void receive();
    },
    stop() {
      stopped = true;
      window.removeEventListener(openEvent, receive);
    },
  };
}
