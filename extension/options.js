import { UI_CLEAR_PREVIEW_MESSAGE_TYPE, UI_DELETE_LOCAL_DATA_MESSAGE_TYPE, UI_RELOAD_TABS_MESSAGE_TYPE } from './runtime/consent-controller.mjs';
import { createSurfaceClient, openSurface, send } from './ui/surface-client.mjs';
import { phaseLabel } from './ui/presentation.mjs';
import { element, show, text, renderLoading, renderReadiness, renderLegalLinks, createPageActions } from './ui/dom.mjs';
import { transition, restoreAccess, allowHistory } from './ui/actions.mjs';

let failed = false;
const client = createSurfaceClient((model) => { if (model.status) failed = false; render(model); }, () => { failed = true; render(client.model); });
const page = createPageActions(client, render);
const mutations = ['revoke', 'pause', 'forget-companion', 'history', 'clear-preview', 'delete-local-data', 'restore-access', 'reload-tabs'];
function render(model) {
  const { status, legal } = model;
  renderLoading(status, failed);
  for (const id of ['capture', 'connection', 'data']) show(id, status !== null);
  for (const id of mutations) page.lock(id, !status);
  renderLegalLinks(legal);
  if (!status) return;
  document.querySelector('main').dataset.ready = 'true';
  text('mode-label', phaseLabel(status));
  text('site-access', status.onlyfans_permission ? 'Allowed for onlyfans.com' : 'Not allowed for onlyfans.com');
  show('revoke', status.onlyfans_permission || !['off', 'revoked'].includes(status.consent.mode));
  show('pause', ['preview', 'full', 'paused'].includes(status.consent.mode));
  text('pause', status.consent.mode === 'paused' ? 'Resume' : 'Pause');
  text('pause-title', status.consent.mode === 'paused' ? 'Resume analytics' : 'Pause analytics');
  show('restore-access', status.phase === 'permission_required'); show('reload-tabs', status.reload_required === true);
  text('brain-status', model.desktopRuntimeReachable ? 'Available on this computer.'
    : 'Open the desktop app to manage your analysis and stored messages.');
  renderReadiness(model);
  show('forget-companion', model.pairing.state === 'paired');
  element('forget-companion').closest('.settings-row').classList.toggle('hidden', model.pairing.state !== 'paired');
  show('history', status.phase === 'full' && model.pairing.state === 'paired');
  element('history').closest('.settings-row').classList.toggle('hidden', status.phase !== 'full' || model.pairing.state !== 'paired');
  text('history', status.history_permission ? 'Open history settings' : 'Allow message history');
  text('history-status', status.history_permission
    ? 'Choose which earlier conversations to include in the desktop app.'
    : 'Allow browser access, then choose earlier conversations in the desktop app.');
  text('pending-count', new Intl.NumberFormat().format(status.delivery?.pending_entries ?? 0));
  const drops = Object.values(status.delivery?.capture_drop_counts ?? {}).reduce((sum, count) => sum + (Number.isSafeInteger(count) ? count : 0), 0);
  text('capture-health', status.delivery?.startup_error_code ? 'Full analytics could not start. Open the desktop app, then retry in setup.'
    : drops ? `${new Intl.NumberFormat().format(drops)} updates could not be recorded.` : 'No capture issues reported.');
  text('history-health', status.delivery?.history_error_code ? 'Message history needs attention in the desktop app.' : '');
}
function confirmAction(title, body, label) {
  const dialog = element('confirm-dialog');
  if (dialog.open) return Promise.resolve(false);
  text('confirm-title', title); text('confirm-body', body); text('confirm-action', label);
  dialog.returnValue = '';
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
    dialog.showModal();
  });
}
function destructive(id, title, body, label, operation, success) {
  element(id).addEventListener('click', () => {
    if (page.busy) return;
    void confirmAction(title, body, label).then((confirmed) => { if (confirmed) void page.run(operation, success); });
  });
}
destructive('revoke', 'Revoke site access?',
  'Stops browser collection and removes site access. Saved extension data and desktop-stored messages remain.', 'Revoke access',
  () => transition('revoked', client.model), 'Site access revoked.');
destructive('forget-companion', 'Forget the desktop app?',
  'Ends this connection. Pair again to use Full analytics. Saved extension data and desktop-stored messages remain.', 'Forget',
  () => client.command('forget'), 'The desktop connection has been removed.');
destructive('delete-local-data', 'Delete extension data?',
  'Removes your extension data and saved setup choices, stops collection, revokes site access and disconnects the desktop app. Messages already stored by the desktop app are not deleted.',
  'Delete extension data', () => send({ type: UI_DELETE_LOCAL_DATA_MESSAGE_TYPE }), 'Extension data deleted. Desktop-stored messages are unchanged.');
page.bind('clear-preview', () => send({ type: UI_CLEAR_PREVIEW_MESSAGE_TYPE }), 'Preview counts cleared.');
page.bind('pause', () => {
  if (client.model.status.consent.mode === 'paused' && client.model.legal?.requires_reauthorization) return openSurface('setup');
  return transition(client.model.status.consent.mode === 'paused' ? 'resume' : 'paused', client.model);
});
page.bind('restore-access', () => restoreAccess(client.model));
page.bind('reload-tabs', () => send({ type: UI_RELOAD_TABS_MESSAGE_TYPE }));
page.bind('history', () => client.model.status.history_permission
  ? chrome.tabs.create({ url: client.model.config.history_settings_url }) : allowHistory(client.model));
page.bind('open-setup', () => openSurface('setup'));
page.bind('open-dashboard', () => chrome.tabs.create({ url: client.model.config.dashboard_url }));
page.bind('manage-desktop-data', () => chrome.tabs.create({ url: client.model.config.history_settings_url }));
page.bind('retry-runtime', () => client.sync());
text('version-label', `Conversation Analytics · version ${chrome.runtime.getManifest().version}`);
for (const id of mutations) page.lock(id, true);
function focusSection() {
  const id = location.hash === '#data' ? 'data-title' : location.hash === '#connection' ? 'connection-title' : null;
  if (id) { element(id).scrollIntoView({ block: 'start' }); element(id).focus({ preventScroll: true }); }
}
window.addEventListener('hashchange', focusSection);
void client.start().then(focusSection);
