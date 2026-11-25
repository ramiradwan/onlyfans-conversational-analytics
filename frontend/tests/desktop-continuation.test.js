import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import html from '../onboarding/index.html?raw';

const owners = vi.hoisted(() => ({ client: null, enrollment: null }));
vi.mock('../onboarding/client.js', () => ({
  DesktopSetupClient: class {
    session = { stage: 'runtime' };
    connect = vi.fn();
    wake = vi.fn();
    act = vi.fn();
    launchBrowser = vi.fn();
    stop = vi.fn();
    constructor(options) { Object.assign(this, options); owners.client = this; }
  },
}));
vi.mock('../onboarding/enrollment.js', () => ({
  DesktopEnrollment: class {
    state = { checked: false, enrolled: null, authenticated: false, busy: false, error: null };
    check = vi.fn();
    authenticate = vi.fn();
    stop = vi.fn();
    constructor(options) { Object.assign(this, options); owners.enrollment = this; }
  },
}));
vi.mock('../onboarding/browser-task.js', async importOriginal => ({ ...await importOriginal(), openBrowserTask: vi.fn(), openSetupCode: vi.fn() }));

const ready = () => ({ next_task: 'complete', local_ready: true, full_ready: false,
  facts: { installation: 'verified' }, allowed_actions: [], pending_operations: [], reason: 'none' });
let replace;
let listeners;

function publish(snapshot = ready(), other = {}) {
  owners.client.receive({ snapshot, connected: true, pending: null, error: null, ...other });
}
function authenticate(other = {}) {
  owners.enrollment.state = { checked: true, enrolled: true, authenticated: true, busy: false, error: null, ...other };
  owners.enrollment.receive();
}

beforeEach(async () => {
  vi.resetModules();
  document.documentElement.innerHTML = html;
  replace = vi.fn();
  vi.stubGlobal('location', { hash: '#presentation=11111111-1111-4111-8111-111111111111', replace });
  listeners = vi.spyOn(window, 'addEventListener');
  await import('../onboarding/main.js');
});

afterEach(() => {
  window.dispatchEvent(new Event('pagehide'));
  for (const [type, listener] of listeners.mock.calls) {
    if (type === 'pagehide') window.removeEventListener(type, listener);
  }
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('opens the dashboard once after verified setup and local authentication', () => {
  publish();
  expect(owners.enrollment.check).toHaveBeenCalledOnce();
  expect(replace).not.toHaveBeenCalled();
  authenticate({ busy: true });
  expect(replace).not.toHaveBeenCalled();
  authenticate();
  expect(replace).toHaveBeenCalledExactlyOnceWith('/');
  expect(document.getElementById('task-status').textContent).toBe('Opening dashboard…');
  publish();
  authenticate();
  expect(replace).toHaveBeenCalledOnce();
  expect(owners.client.act).not.toHaveBeenCalled();
  expect(owners.enrollment.authenticate).not.toHaveBeenCalled();
});

it('waits for the authoritative local setup result when authentication finishes first', () => {
  authenticate();
  publish({ ...ready(), next_task: 'finish-setup', local_ready: false });
  expect(replace).not.toHaveBeenCalled();
  publish();
  expect(replace).toHaveBeenCalledExactlyOnceWith('/');
});

it.each([
  ['disconnected snapshot', {}, { connected: false }],
  ['failed status read', {}, { error: 'status_unavailable' }],
  ['expired presentation', {}, { error: 'session_unavailable' }],
  ['incomplete local setup', { local_ready: false }, {}],
  ['unfinished setup task', { next_task: 'finish-setup' }, {}],
])('does not navigate from %s', (_name, snapshot, state) => {
  authenticate();
  publish({ ...ready(), ...snapshot }, state);
  expect(replace).not.toHaveBeenCalled();
});

it.each([
  ['unread authentication', { checked: false }],
  ['signed-out operator', { authenticated: false }],
  ['failed authentication read', { authenticated: false, error: 'passkey_status_unavailable' }],
])('keeps %s in the local authentication flow', (_name, state) => {
  authenticate(state);
  publish();
  expect(replace).not.toHaveBeenCalled();
  expect(owners.enrollment.authenticate).not.toHaveBeenCalled();
});

it('requires the configured runtime even if stale authentication is present', () => {
  owners.client.session.stage = 'setup';
  authenticate();
  publish();
  expect(replace).not.toHaveBeenCalled();
});

it('continues with unresolved optional activation without creating or reconciling it', () => {
  authenticate();
  publish({ ...ready(), allowed_actions: ['activation.review'],
    decision_status: { state: 'accepted' },
    pending_operations: [{ action: 'activation.review', state: 'outcome_unknown', reason: 'none' }] });
  expect(replace).toHaveBeenCalledExactlyOnceWith('/');
  expect(document.getElementById('task-reconcile')).toBeNull();
  expect(owners.client.act).not.toHaveBeenCalled();
});

it('waits for a new authenticated snapshot before offering the explicit extension connection', async () => {
  const snapshot = { ...ready(), next_task: 'finish-setup', local_ready: false, allowed_actions: ['browser.pair'] };
  const button = document.getElementById('task-action');
  publish(snapshot);
  expect(button.hidden).toBe(true);
  button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  expect(owners.client.act).not.toHaveBeenCalled();
  owners.enrollment.refresh();
  authenticate();
  expect(button.hidden).toBe(true);
  button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  expect(owners.client.act).not.toHaveBeenCalled();
  publish(snapshot);
  expect(button.hidden).toBe(false);
  expect(button.textContent).toBe('Connect extension');
  expect(owners.client.act).not.toHaveBeenCalled();
  button.click();
  await Promise.resolve();
  expect(owners.client.act).toHaveBeenCalledExactlyOnceWith('browser.pair', {});
});
it('does not navigate from the old setup snapshot while authenticated status is being refreshed', () => {
  publish();
  owners.enrollment.refresh();
  authenticate();
  expect(replace).not.toHaveBeenCalled();
  publish();
  expect(replace).toHaveBeenCalledExactlyOnceWith('/');
});
it('opens the exact operation rendered on the current browser recovery button', async () => {
  owners.client.session.stage = 'setup';
  const selected = '22222222-2222-4222-8222-222222222222';
  const other = '33333333-3333-4333-8333-333333333333';
  publish({ ...ready(), next_task: 'browser-access', local_ready: false, pending_operations: [
    { operation_id: other, action: 'activation.review', state: 'waiting', reason: 'none' },
    { operation_id: selected, action: 'browser.access', state: 'waiting', reason: 'none' },
  ] }, { pending: other });
  const button = document.getElementById('browser-reopen');
  expect(button.hidden).toBe(false);
  button.click();
  await Promise.resolve();
  expect(owners.client.launchBrowser).toHaveBeenCalledExactlyOnceWith(true, selected);
  publish({ ...ready(), next_task: 'browser-access', local_ready: false });
  button.click();
  expect(owners.client.launchBrowser).toHaveBeenCalledOnce();
});
it('ignores late completion after the setup page has closed', () => {
  publish();
  window.dispatchEvent(new Event('pagehide'));
  authenticate();
  publish();
  expect(replace).not.toHaveBeenCalled();
  expect(owners.client.stop).toHaveBeenCalledOnce();
  expect(owners.enrollment.stop).toHaveBeenCalledOnce();
});

it('marks the clicked provider busy at once and keeps it busy while its sign-in is accepted', async () => {
  const operation = '22222222-2222-4222-8222-222222222222';
  const signIn = { operation_id: null, browser_operation_id: null, state: 'selection-required',
    choices: { google: true, 'microsoft-personal': true, telegram: true, tiktok: false, default: true },
    actions: { select: true, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null };
  const choosing = { ...ready(), next_task: 'account-connection', local_ready: false, allowed_actions: ['account.connect'], authentication: signIn };
  let settle;
  owners.client.session.stage = 'setup';
  owners.client.act.mockImplementation(() => new Promise(resolve => { settle = resolve; }));
  publish(choosing);
  const busy = () => [...document.querySelectorAll('[data-provider]')].filter(button => button.getAttribute('aria-busy') === 'true')
    .map(button => button.dataset.provider);
  document.querySelector('[data-provider="telegram"]').click();
  expect(owners.client.act).toHaveBeenCalledExactlyOnceWith('account.connect', { provider: 'telegram' });
  expect(busy()).toEqual(['telegram']);
  const accepted = { ...choosing, allowed_actions: [], authentication: null,
    pending_operations: [{ operation_id: operation, action: 'account.connect', state: 'accepted', reason: 'none' }] };
  publish(accepted, { pending: operation });
  settle(); await Promise.resolve(); await Promise.resolve();
  expect(busy()).toEqual(['telegram']);
  publish(choosing);
  expect(busy()).toEqual([]);
});

it('wakes setup reconnection on focus and visibility without a Retry button', () => {
  expect(document.getElementById('task-reconnect')).toBeNull();
  window.dispatchEvent(new Event('focus'));
  expect(owners.client.wake).toHaveBeenCalledTimes(1);
  Object.defineProperty(document, 'hidden', { value: true, configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
  expect(owners.client.wake).toHaveBeenCalledTimes(1);
  Object.defineProperty(document, 'hidden', { value: false, configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
  expect(owners.client.wake).toHaveBeenCalledTimes(2);
  window.dispatchEvent(new Event('pagehide'));
  window.dispatchEvent(new Event('focus'));
  document.dispatchEvent(new Event('visibilitychange'));
  expect(owners.client.wake).toHaveBeenCalledTimes(2);
});

it('renders R14 without a Retry button when the presentation fragment is missing', async () => {
  window.dispatchEvent(new Event('pagehide'));
  vi.resetModules();
  document.documentElement.innerHTML = html;
  vi.stubGlobal('location', { hash: '', replace });
  await import('../onboarding/main.js');
  expect(document.querySelector('main').dataset.journeyState).toBe('desktop.setup_unavailable');
  expect(document.getElementById('task-title').textContent).toBe('Setup couldn’t continue');
  expect(document.getElementById('task-status').dataset.tone).toBe('none');
  expect(document.getElementById('task-reconnect')).toBeNull();
});

it('clicks a verified provider in ceremony replacement and prepared source continuation without putting provider on the replacement command', async () => {
  const parent = '22222222-2222-4222-8222-222222222222';
  const choices = { google: true, 'microsoft-personal': false, telegram: false, tiktok: false, default: true };
  const busy = () => [...document.querySelectorAll('[data-provider]')].filter(button => button.getAttribute('aria-busy') === 'true')
    .map(button => button.dataset.provider);
  owners.client.session.stage = 'setup';
  let settle;
  owners.client.act.mockImplementation(() => new Promise(resolve => { settle = resolve; }));

  // Path 1: ceremony replacement with retained predecessor choices.
  publish({ ...ready(), journey_id: parent, next_task: 'account-connection', local_ready: false,
    allowed_actions: ['auth.select'], pending_operations: [{ operation_id: parent, action: 'account.connect', state: 'waiting', reason: 'none' }],
    authentication: { operation_id: parent, browser_operation_id: null, state: 'replacement-required', choices,
      actions: { select: true, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null } });
  expect(document.getElementById('task-action').hidden).toBe(true);
  document.querySelector('[data-provider="google"]').click();
  expect(owners.client.act).toHaveBeenCalledExactlyOnceWith('auth.select', { replace_ceremony: true }, { continueProvider: 'google' });
  expect(busy()).toEqual(['google']);
  publish({ ...ready(), journey_id: parent, next_task: 'account-connection', local_ready: false,
    allowed_actions: [], pending_operations: [{ operation_id: parent, action: 'account.connect', state: 'waiting', reason: 'none' }],
    authentication: { operation_id: parent, browser_operation_id: null, state: 'replacement-pending',
      choices: { google: false, 'microsoft-personal': false, telegram: false, tiktok: false, default: false },
      actions: { select: false, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null } },
  { pending: '33333333-3333-4333-8333-333333333333', continuingProvider: 'google' });
  settle(); await Promise.resolve(); await Promise.resolve();
  expect(busy()).toEqual(['google']);
  expect(document.getElementById('task-status').textContent).toBe('Preparing sign-in\u2026');

  // Path 2: prepared source continuation with retained predecessor choices.
  owners.client.act.mockClear();
  publish({ ...ready(), journey_id: parent, next_task: 'account-connection', local_ready: false,
    continuation_available: true, allowed_actions: ['account.connect'], pending_operations: [],
    authentication: { operation_id: null, browser_operation_id: null, state: 'replacement-required', choices,
      actions: { select: false, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null } });
  expect(document.getElementById('task-action').hidden).toBe(true);
  document.querySelector('[data-provider="default"]').click();
  expect(owners.client.act).toHaveBeenCalledExactlyOnceWith('account.connect', {}, { continueProvider: 'default' });
  expect(busy()).toEqual(['default']);
  settle(); await Promise.resolve();
});

it('opens the dashboard after current readiness supersedes an old action refusal', () => {
  authenticate();
  publish(ready(), { error: 'operation_expired', failedAction: 'browser.pair' });
  expect(replace).toHaveBeenCalledExactlyOnceWith('/');
});
