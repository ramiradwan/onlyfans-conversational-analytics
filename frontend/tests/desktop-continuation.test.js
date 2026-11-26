import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import html from '../onboarding/index.html?raw';

const owners = vi.hoisted(() => ({ client: null, enrollment: null }));
vi.mock('../onboarding/client.js', () => ({
  DesktopSetupClient: class {
    session = { stage: 'runtime' };
    connect = vi.fn(() => { this.connected = false; this.launchEpoch++; });
    wake = vi.fn();
    act = vi.fn(async () => ({ state: 'accepted' }));
    json = vi.fn();
    notify = vi.fn();
    takeReturnedPasskey = vi.fn(() => null);
    discardReturnedPasskey = vi.fn();
    launchEpoch = 0;
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
  owners.client.value = snapshot;
  owners.client.connected = other.connected ?? true;
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
  sessionStorage.removeItem('desktop-presentation');
  document.documentElement.removeAttribute('data-account-passkey');
});

function passkeyOffer() {
  const generation = owners.client.generation;
  const operationId = '22222222-2222-4222-8222-222222222222';
  return { ...ready(), generation, revision: 7, scope_digest: 'a'.repeat(64),
    journey_id: '33333333-3333-4333-8333-333333333333', next_task: 'account-connection', local_ready: false,
    account_passkey: 'available', allowed_actions: ['account.confirm'],
    decision: { operation_id: operationId, action: 'connect-account', creator_id: 'test-creator',
      computer_name: 'Test computer', expires_at: '2038-01-01T00:00:00Z' },
    authentication: { operation_id: operationId, state: 'admitted', choices: {},
      actions: { select: false, cancel: false, open_browser: false, switch_account: false },
      authentication_attempt: { authentication_attempt_id: '0199b234-5678-7000-8000-000000000002', provider: 'google', mode: 'sign-in' } },
  };
}

function pendingSignIn() {
  const parent = '22222222-2222-4222-8222-222222222222';
  const child = '44444444-4444-4444-8444-444444444444';
  return { ...ready(), generation: owners.client.generation, revision: 7, scope_digest: 'a'.repeat(64),
    expires_at: Math.floor(Date.now() / 1000) + 60, journey_id: '33333333-3333-4333-8333-333333333333',
    next_task: 'account-connection', local_ready: false, allowed_actions: ['auth.cancel'],
    pending_operations: [
      { operation_id: parent, action: 'account.connect', state: 'waiting', reason: 'none' },
      { operation_id: child, action: 'auth.select', state: 'outcome_unknown', reason: 'operation_unconfirmed' },
    ],
    authentication: { operation_id: parent, browser_operation_id: child, state: 'selected',
      choices: { google: false, 'microsoft-personal': false, telegram: false, tiktok: false, default: true },
      actions: { select: false, cancel: true, open_browser: true, switch_account: false },
      authentication_attempt: { authentication_attempt_id: '0199b234-5678-7000-8000-000000000002', provider: 'default', mode: 'sign-in' } },
  };
}

it('cancels the current ordinary sign-in once and waits for the owner to admit retry', async () => {
  owners.client.session.stage = 'setup';
  const selected = pendingSignIn();
  const child = selected.authentication.browser_operation_id;
  let complete;
  owners.client.act.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  publish(selected, { pending: child });

  const cancel = document.getElementById('sign-in-cancel');
  const options = document.getElementById('sign-in-options');
  expect(cancel?.hidden).toBe(false);
  expect(cancel.textContent).toBe('Cancel sign-in');
  expect(cancel.disabled).toBe(false);
  expect(document.getElementById('protected-cancel').hidden).toBe(true);
  expect(options.hidden).toBe(true);
  cancel.click();
  expect(cancel.disabled).toBe(true);
  cancel.click();
  expect(owners.client.act).toHaveBeenCalledExactlyOnceWith('auth.cancel', {});
  expect(owners.client.launchBrowser).not.toHaveBeenCalled();

  const retired = { ...selected, revision: 8, allowed_actions: [],
    pending_operations: [selected.pending_operations[0]],
    authentication: { ...selected.authentication, state: 'retired',
      actions: { select: true, cancel: false, open_browser: false, switch_account: false } } };
  publish(retired);
  complete({ state: 'completed' });
  await Promise.resolve();
  expect(cancel.hidden).toBe(true);
  expect(options.hidden).toBe(true);

  publish({ ...retired, revision: 9, allowed_actions: ['auth.select'] });
  const retry = options.querySelector('[data-provider="default"]');
  expect(options.hidden).toBe(false);
  expect(retry.hidden).toBe(false);
  expect(retry.disabled).toBe(false);
  expect(retry.textContent).toBe('Other sign-in options');
  expect(retry.dataset.action).toBe('auth.select');
  expect(owners.client.act).toHaveBeenCalledTimes(1);
});

it('fences ordinary sign-in cancellation when the displayed action expires or becomes unavailable', () => {
  owners.client.session.stage = 'setup';
  const selected = pendingSignIn();
  const { authentication } = selected;
  const parent = authentication.operation_id;
  const child = authentication.browser_operation_id;
  for (const pending of [null, parent, child]) {
    publish(selected, { pending });
    expect(document.getElementById('sign-in-cancel')?.hidden).toBe(false);
  }
  const cancel = document.getElementById('sign-in-cancel');
  const clock = vi.spyOn(Date, 'now').mockReturnValue(selected.expires_at * 1000);
  cancel.click();
  expect(owners.client.act).not.toHaveBeenCalled();
  publish(selected, { pending: child });
  expect(cancel.hidden).toBe(true);
  clock.mockRestore();
  for (const [value, state] of [
    [selected, { connected: false, pending: child }],
    [selected, { pending: '55555555-5555-4555-8555-555555555555' }],
    [{ ...selected, pending_operations: [
      { ...selected.pending_operations[0], state: 'outcome_unknown' },
      { ...selected.pending_operations[1], state: 'waiting', reason: 'none' },
    ] }, { pending: child }],
    [{ ...selected, allowed_actions: [] }, { pending: child }],
    [{ ...selected, authentication: { ...authentication, actions: { ...authentication.actions, cancel: false } } }, { pending: child }],
    [{ ...selected, authentication: { ...authentication, authentication_attempt: null } }, { pending: child }],
    [{ ...selected, authority_recovery: 'sign-in-required' }, { pending: parent }],
  ]) {
    publish(value, state);
    expect(cancel.hidden).toBe(true);
    cancel.click();
    expect(owners.client.act).not.toHaveBeenCalled();
  }
});

it.each([true, false])('only dispatches enrollment from a foreground return and leaves Connect explicit (foreground=%s)', async foreground => {
  owners.client.session.stage = 'setup';
  const offered = passkeyOffer();
  sessionStorage.setItem('desktop-presentation', offered.generation);
  const invoke = vi.fn(async (_command, { action }) => action === 'passkey_foreground' && foreground);
  vi.stubGlobal('__TAURI__', { core: { invoke } });
  owners.client.takeReturnedPasskey.mockReturnValueOnce(Date.now() + 60_000);
  owners.client.json.mockResolvedValue({ outcome: 'created' });
  publish(offered);
  await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('desktop_window', { action: 'passkey_foreground' }));
  if (foreground) await vi.waitFor(() => expect(owners.client.json).toHaveBeenCalledOnce());
  else expect(owners.client.json).not.toHaveBeenCalled();
  if (foreground) expect(owners.client.json).toHaveBeenCalledWith('/api/v1/desktop/setup/passkey', {
    generation: offered.generation, revision: offered.revision, scope_digest: offered.scope_digest,
  });
  publish({ ...offered, account_passkey: foreground ? 'created' : 'available' });
  window.dispatchEvent(new Event('focus'));
  expect(owners.client.json).toHaveBeenCalledTimes(foreground ? 1 : 0);
  expect(owners.client.act).not.toHaveBeenCalled();
  await vi.waitFor(() => expect(document.getElementById(foreground ? 'task-action' : 'account-passkey-create').hidden).toBe(false));
});

it('retains the original enrollment deadline while checking native foreground', async () => {
  owners.client.session.stage = 'setup';
  const offered = passkeyOffer();
  sessionStorage.setItem('desktop-presentation', offered.generation);
  let resolve;
  const checked = new Promise(done => { resolve = done; });
  vi.stubGlobal('__TAURI__', { core: { invoke: (_command, { action }) =>
    action === 'passkey_foreground' ? checked : Promise.resolve(false) } });
  const now = Date.now();
  const clock = vi.spyOn(Date, 'now').mockReturnValue(now);
  owners.client.takeReturnedPasskey.mockReturnValueOnce(now + 1);
  publish(offered);
  clock.mockReturnValue(now + 1);
  resolve(true); await checked; await Promise.resolve();
  expect(owners.client.json).not.toHaveBeenCalled();
  expect(document.getElementById('account-passkey-create').hidden).toBe(false);
});

it('pauses and resumes setup through the durable scoped actions', async () => {
  owners.client.session.stage = 'setup';
  const offered = passkeyOffer();
  owners.client.json.mockResolvedValueOnce({ outcome: 'paused' }).mockResolvedValueOnce({ outcome: 'resumed' });
  publish(offered);
  document.getElementById('account-passkey-later').click();
  expect(owners.client.json).toHaveBeenCalledExactlyOnceWith('/api/v1/desktop/setup/passkey/pause', {
    generation: offered.generation, revision: offered.revision, scope_digest: offered.scope_digest,
  });
  await Promise.resolve();
  publish({ ...offered, revision: 8, decision: undefined, allowed_actions: [], account_passkey: 'paused' });
  const resume = document.getElementById('account-passkey-resume');
  expect(resume.hidden).toBe(false);
  expect(document.getElementById('task-action').hidden).toBe(true);
  resume.click();
  expect(owners.client.json).toHaveBeenLastCalledWith('/api/v1/desktop/setup/passkey/resume', {
    generation: offered.generation, revision: 8, scope_digest: offered.scope_digest,
  });
  await Promise.resolve();
  publish({ ...offered, revision: 9, account_passkey: 'recoverable', allowed_actions: [] });
  expect(document.getElementById('account-passkey-create').textContent).toBe('Resume passkey setup');
  expect(owners.client.json).toHaveBeenCalledTimes(2);
  expect(owners.client.act).not.toHaveBeenCalled();
});

it('tries initial local access only on the uninterrupted promotion before offering pairing', () => {
  const offered = passkeyOffer();
  sessionStorage.setItem('desktop-presentation', offered.generation);
  vi.stubGlobal('__TAURI__', { core: { invoke: vi.fn() } });
  owners.client.session.stage = 'setup';
  publish(offered);
  expect(owners.enrollment.check).not.toHaveBeenCalled();
  owners.client.session.stage = 'runtime';
  const pairing = { ...ready(), generation: offered.generation, journey_id: offered.journey_id,
    next_task: 'finish-setup', local_ready: false, allowed_actions: ['browser.pair'] };
  publish(pairing);
  const initial = owners.enrollment.check.mock.calls[0][0].initial;
  expect(initial()).toBe(true);
  expect(owners.enrollment.authenticate).not.toHaveBeenCalled();
  expect(document.getElementById('task-action').hidden).toBe(true);
  owners.enrollment.refresh();
  expect(initial()).toBe(false);
  authenticate();
  publish(pairing);
  expect(document.getElementById('task-action').textContent).toBe('Connect extension');
  expect(owners.enrollment.check).toHaveBeenCalledOnce();
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
