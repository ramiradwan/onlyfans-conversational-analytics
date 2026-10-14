import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import html from '../onboarding/index.html?raw';

const owners = vi.hoisted(() => ({ client: null, enrollment: null }));
vi.mock('../onboarding/client.js', () => ({
  DesktopSetupClient: class {
    session = { stage: 'runtime' };
    connect = vi.fn();
    act = vi.fn();
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
vi.mock('../onboarding/browser-task.js', () => ({ openBrowserTask: vi.fn(), openSetupCode: vi.fn() }));

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

it('ignores late completion after the setup page has closed', () => {
  publish();
  window.dispatchEvent(new Event('pagehide'));
  authenticate();
  publish();
  expect(replace).not.toHaveBeenCalled();
  expect(owners.client.stop).toHaveBeenCalledOnce();
  expect(owners.enrollment.stop).toHaveBeenCalledOnce();
});
