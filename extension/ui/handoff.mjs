import { DESKTOP_HANDOFF_WINDOW_KEY } from '../runtime/ui-surfaces.mjs';

// Only a compact window opened for the desktop app closes itself.
export function createHandoffFinisher() {
  let finishing = false;
  return async function finish() {
    if (finishing) return;
    finishing = true;
    try {
      const current = await chrome.windows.getCurrent();
      const saved = (await chrome.storage.session.get([DESKTOP_HANDOFF_WINDOW_KEY]))[DESKTOP_HANDOFF_WINDOW_KEY];
      if (current?.type !== 'popup' || saved?.window_id !== current.id || saved.url !== location.href) {
        finishing = false;
        return;
      }
      await chrome.windows.remove(current.id);
      const latest = (await chrome.storage.session.get([DESKTOP_HANDOFF_WINDOW_KEY]))[DESKTOP_HANDOFF_WINDOW_KEY];
      if (latest?.window_id === saved.window_id && latest.url === saved.url) {
        await chrome.storage.session.remove([DESKTOP_HANDOFF_WINDOW_KEY]);
      }
    } catch { finishing = false; }
  };
}
