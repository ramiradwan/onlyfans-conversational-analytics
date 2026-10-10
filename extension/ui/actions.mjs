import { ONLYFANS_ORIGIN_PATTERN, UI_TRANSITION_MESSAGE_TYPE } from '../runtime/consent-controller.mjs';
import { LEGAL_CHOOSE_MODE_MESSAGE_TYPE } from '../runtime/legal-activation-controller.mjs';
import { requiredOriginsForMode } from '../runtime/permission-recovery.mjs';
import { NoticeError, send } from './surface-client.mjs';

export async function requestAnalyticsAccess(mode) {
  const origins = requiredOriginsForMode(mode);
  if (!origins.length) throw new NoticeError('Choose an analytics mode in setup first.');
  const granted = await chrome.permissions.request({ origins });
  if (!granted) throw new NoticeError('Site access was not allowed, so nothing was turned on.');
}
export async function chooseMode(mode) {
  await requestAnalyticsAccess(mode);
  return send({ type: LEGAL_CHOOSE_MODE_MESSAGE_TYPE, mode });
}
export async function transition(mode, model) {
  if (mode === 'resume') {
    if (!model.legal?.configured || model.legal.requires_reauthorization) throw new NoticeError('Review the changes in setup before restarting analytics.');
    await requestAnalyticsAccess(model.status?.consent.resume_mode);
  }
  return send({ type: UI_TRANSITION_MESSAGE_TYPE, mode });
}
export async function restoreAccess(model) {
  await requestAnalyticsAccess(model.status?.consent.mode);
  return send({ type: UI_TRANSITION_MESSAGE_TYPE, mode: model.status.consent.mode });
}
export async function allowHistory(model) {
  const granted = await chrome.permissions.request({ permissions: ['webRequest'], origins: [ONLYFANS_ORIGIN_PATTERN] });
  if (!granted) throw new NoticeError('Message history access was not allowed.');
  return chrome.tabs.create({ url: model.config.history_settings_url });
}

export async function openCreatorAccount(chromeApi = globalThis.chrome) {
  let tabs;
  try { tabs = await chromeApi.tabs.query({ url: [ONLYFANS_ORIGIN_PATTERN] }); } catch { return; }
  const tab = tabs.find((candidate) => candidate.active === true) ?? tabs[0];
  if (Number.isInteger(tab?.id)) {
    try {
      await chromeApi.tabs.update(tab.id, { active: true });
      if (Number.isInteger(tab.windowId)) await chromeApi.windows?.update?.(tab.windowId, { focused: true });
    } catch {}
    return;
  }
  await chromeApi.tabs.create({ url: 'https://onlyfans.com/' });
}
