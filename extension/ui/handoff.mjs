import { DESKTOP_HANDOFF_STORAGE_KEY } from '../runtime/ui-surfaces.mjs';

// Return to the desktop app's tab recorded when this compact window opened.
export async function returnToDesktop(dashboardUrl) {
  let anchor = null;
  try { anchor = (await chrome.storage.session.get([DESKTOP_HANDOFF_STORAGE_KEY]))[DESKTOP_HANDOFF_STORAGE_KEY] ?? null; } catch {}
  if (Number.isInteger(anchor?.tab_id) && Number.isInteger(anchor?.window_id)) {
    try {
      await chrome.tabs.update(anchor.tab_id, { active: true });
      await chrome.windows.update(anchor.window_id, { focused: true });
      return true;
    } catch {}
  }
  await chrome.tabs.create({ url: dashboardUrl });
  return false;
}

// Only a compact window opened for the desktop app closes itself.
export function createHandoffFinisher(dashboardUrl) {
  let finishing = false;
  return async function finish() {
    if (finishing) return;
    finishing = true;
    try {
      const current = await chrome.windows.getCurrent();
      if (current?.type !== 'popup') return;
      await returnToDesktop(dashboardUrl());
      await chrome.windows.remove(current.id);
    } catch { finishing = false; }
  };
}
