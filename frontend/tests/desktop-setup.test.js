import { afterEach, describe, expect, it, vi } from 'vitest';
import { runInNewContext } from 'node:vm';
import { readFileSync } from 'node:fs';
import generatedContract from '../onboarding/generated-contract.js?raw';
import setupThemeInitializer from '../public/setup-theme-init.js?raw';
import markup from '../onboarding/index.html?raw';
import { DesktopSetupClient } from '../onboarding/client.js';
import { snapshot } from '../onboarding/contract.js';
import { setupEvents } from '../onboarding/events.js';
import { openProtectedBrowser } from '../onboarding/browser-task.js';
import { authorityRecoveryView, authorityRecoveryOptions } from '../shared-setup/authority-recovery.js';
import { renderSetup } from '../onboarding/view.js';
import { assertSetupIsolation, assertDashboardIsolation, assertReceivingIsolation } from '../scripts/setup-isolation.js';
import { applyTheme } from '../shared-setup/presentation.js';

const generation = '11111111-1111-4111-8111-111111111111';
const entryScript = readFileSync('../app/onboarding/entry.js', 'utf8');
const operationId = '22222222-2222-4222-8222-222222222222';
const record = { operation_id: operationId, action: 'browser.access', state: 'accepted', reason: 'none' };
const initial = () => ({ profile: 'desktop-onboarding.v2', kind: 'snapshot', journey_id: null,
  generation, revision: 0, scope_digest: 'a'.repeat(64), expires_at: 2000000000,
  facts: Object.fromEntries(['browser_access', 'installation', 'account', 'configuration', 'enrollment', 'pairing', 'activation', 'analysis'].map(name => [name, 'missing'])),
  next_task: 'browser-access', local_ready: false, full_ready: false, pending_operations: [], allowed_actions: ['browser.access'], reason: 'none',
  analysis_readiness: { availability: 'unauthorized', creator_account_id: null, value: null } });
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
const session = { role: 'desktop', stage: 'setup', generation, csrf_token: 'A'.repeat(43) };
const storage = () => { const values = new Map(); return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }; };

it('sends closed click deltas without delaying command dispatch', async () => {
  const calls = [];
  const subject = new DesktopSetupClient({ generation, storage: storage(), receive() {},
    uuid: () => operationId, stream: () => () => {}, request: async (url, options) => {
      calls.push([url, options?.body && JSON.parse(options.body)]);
      if (url.endsWith('/timing')) return new Promise(() => {});
      if (url.includes('/operations/')) return response(record);
      return response(record, 202);
    } });
  subject.value = initial(); subject.session = session; subject.connected = true;
  await subject.act('browser.access');
  const phases = calls.filter(([url]) => url.endsWith('/timing')).map(([, body]) => body);
  expect(phases.map(value => value.phase)).toEqual(['click', 'command-send']);
  for (const value of phases) {
    expect(Object.keys(value).sort()).toEqual(['elapsed_ms', 'operation', 'phase', 'status']);
    expect(value.operation).toBe('frontend');
  }
  expect(calls.some(([url]) => url.endsWith('/commands'))).toBe(true);
});

it('records when its click finds the browser task and when Chrome was launched, once each', async () => {
  const calls = [];
  const subject = new DesktopSetupClient({ generation, storage: storage(), receive() {},
    uuid: () => operationId, stream: () => () => {},
    openBrowser: async (owner, selected) => {
      owner.browserTaskPrepared({ operation_id: selected, journey_id: '33333333-3333-4333-8333-333333333333', expires_at: 2000000000 });
      return true;
    },
    request: async (url, options) => {
      calls.push([url, options?.body && JSON.parse(options.body)]);
      if (url.endsWith('/timing')) return new Promise(() => {});
      if (url.includes('/operations/')) return response(record);
      return response(record, 202);
    } });
  subject.value = initial(); subject.session = session; subject.connected = true;
  await subject.act('browser.access');
  subject.value = { ...subject.value, pending_operations: [record] };
  subject.remember(operationId);
  await subject.launchBrowser(true, operationId);
  const phases = calls.filter(([url]) => url.endsWith('/timing')).map(([, body]) => body.phase);
  expect(phases).toEqual(['click', 'command-send', 'task-found', 'launch']);
});

it('launches the browser for an accepted click while its receipt is still being read', async () => {
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const openBrowser = vi.fn(async () => true);
  const subject = new DesktopSetupClient({ generation, storage: storage(), receive() {},
    uuid: () => operationId, stream: () => () => {}, openBrowser,
    request: async (url) => {
      if (url.endsWith('/timing')) return new Promise(() => {});
      if (url.includes('/operations/')) { await held; return response(record); }
      return response(record, 202);
    } });
  subject.value = initial(); subject.session = session; subject.connected = true;
  const acting = subject.act('browser.access');
  await vi.waitFor(() => expect(openBrowser).toHaveBeenCalledTimes(1));
  release();
  await acting;
  expect(openBrowser.mock.calls[0][1]).toBe(operationId);
});

it('stops a launch still in progress when its receipt is refused', async () => {
  let found;
  const lookup = new Promise(resolve => { found = resolve; });
  const invoke = vi.fn();
  const receive = vi.fn();
  const subject = new DesktopSetupClient({ generation, storage: storage(), receive,
    uuid: () => operationId, stream: () => () => {},
    openBrowser: async (owner, selected, _, current) => {
      await lookup;
      if (!current()) return false;
      invoke(selected);
      return true;
    },
    request: async (url) => {
      if (url.endsWith('/timing')) return new Promise(() => {});
      if (url.includes('/operations/')) return response({ ...record, state: 'refused', reason: 'operation_expired' });
      if (url.endsWith('/session')) return new Promise(() => {});
      return response(record, 202);
    } });
  subject.value = initial(); subject.session = session; subject.connected = true;
  const acting = subject.act('browser.access');
  await vi.waitFor(() => expect(subject.launchOperation).toBe(null));
  found();
  await acting;
  expect(invoke).not.toHaveBeenCalled();
  expect(subject.pending).toBe(null);
});

it('selects the saved setup theme before the entry module can paint', () => {
  const meta = { setAttribute: vi.fn() };
  const earlyDocument = { documentElement: { dataset: {} }, querySelector: () => meta };
  runInNewContext(setupThemeInitializer, {document: earlyDocument, localStorage: {getItem: () => 'dark'}});
  expect(earlyDocument.documentElement.dataset.colorScheme).toBe('dark');
  expect(meta.setAttribute).toHaveBeenCalledWith('content', 'dark');
  expect(markup.indexOf('src="/setup-theme-init.js"')).toBeGreaterThan(0);
  expect(markup.indexOf('src="/setup-theme-init.js"')).toBeLessThan(markup.indexOf('type="module"'));
  const denied = {document: {documentElement: {dataset: {}}}};
  Object.defineProperty(denied, 'localStorage', {get: () => {throw new Error('denied');}});
  expect(() => runInNewContext(setupThemeInitializer, denied)).not.toThrow();
  expect(denied.document.documentElement.dataset.colorScheme).toBeUndefined();
});

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.documentElement.removeAttribute('data-color-scheme'); });

describe('desktop setup projection', () => {
  it('re-admits an expired native presentation and reads its exact pending operation without replay', async () => {
    const nextGeneration = '33333333-3333-4333-8333-333333333333';
    const receive = vi.fn(), refreshPresentation = vi.fn(async () => 'B'.repeat(43)), openBrowser = vi.fn();
    const saved = storage();
    let reads = 0;
    const request = vi.fn(async path => {
      if (path.endsWith('/session') && ++reads === 1) return response({}, 401);
      if (path.endsWith('/entry/consume')) return response({ ...session, generation: nextGeneration, journey_id: null });
      return response({ ...record, state: 'outcome_unknown' });
    });
    const client = new DesktopSetupClient({ generation, storage: saved, receive, request, refreshPresentation, openBrowser,
      stream: async (_request, headers, _signal, publish) => {
        expect(headers['X-Desktop-Generation']).toBe(nextGeneration);
        publish(JSON.stringify({ ...initial(), generation: nextGeneration, pending_operations: [{ ...record, state: 'outcome_unknown' }] }));
      } });
    client.remember(operationId);
    await client.connect();
    expect(refreshPresentation).toHaveBeenCalledExactlyOnceWith(generation);
    expect(saved.getItem('desktop-presentation')).toBe(nextGeneration);
    expect(saved.getItem('desktop-setup-operation:' + nextGeneration)).toBe(operationId);
    expect(client.pending).toBe(operationId);
    expect(client.connected).toBe(true);
    expect(openBrowser).not.toHaveBeenCalled();
    expect(request.mock.calls.filter(([, options]) => options.method === 'POST').map(([path]) => path)).toEqual(['/api/v1/desktop/entry/consume']);
    client.stop();
  });
  it('retains a refused activation result across subsequent status snapshots', async () => {
    let publish;
    const request = vi.fn(async path => response(path.endsWith('/session') ? session
      : { ...record, action: 'activation.review', state: 'refused', reason: 'activation_unavailable' }));
    const receive = vi.fn();
    const client = new DesktopSetupClient({ generation, request, receive, storage: storage(),
      stream: async (_request, _headers, _signal, listener) => { publish = listener; listener(JSON.stringify(initial())); } });
    await client.connect(); client.remember(operationId); await client.receipt();
    publish(JSON.stringify({ ...initial(), revision: 1 }));
    expect(receive).toHaveBeenLastCalledWith(expect.objectContaining({ error: 'activation_unavailable', connected: true }));
    client.stop();
  });
  it.each(['same-admission', 'same-review', 'foreign-review'])('retires authentication feedback only for %s', async result => {
    let publish;
    const parent = '33333333-3333-4333-8333-333333333333';
    const value = { ...initial(), next_task: 'account-connection', authentication: { operation_id: parent,
      browser_operation_id: operationId, state: 'selected', choices: { google: true, 'microsoft-personal': true, telegram: false, tiktok: false, default: true },
      authentication_attempt: { authentication_attempt_id: '0199b234-5678-7000-8000-000000000002', provider: 'default', mode: 'sign-in' },
      actions: { select: false, cancel: true, open_browser: true, switch_account: false } } };
    const receive = vi.fn(), request = vi.fn(async path => response(path.endsWith('/session') ? session
      : { ...record, action: 'auth.select', state: 'refused', reason: 'operation_unconfirmed' }));
    const client = new DesktopSetupClient({ generation, request, receive, storage: storage(),
      stream: async (_request, _headers, _signal, listener) => { publish = next => listener(JSON.stringify(next)); publish(value); } });
    await client.connect(); client.remember(operationId); await client.receipt();
    publish({ ...value, revision: 1 });
    expect(receive).toHaveBeenLastCalledWith(expect.objectContaining({ error: 'operation_unconfirmed' }));
    const next = { ...value, revision: 2 };
    if (result === 'same-admission') next.authentication = { ...value.authentication, state: 'admitted' };
    else next.decision = { operation_id: result === 'same-review' ? parent : '44444444-4444-4444-8444-444444444444',
      action: 'connect-account', creator_id: 'synthetic-creator', computer_name: 'This computer', expires_at: '2030-01-01T00:00:00.000Z' };
    publish(next);
    expect(receive).toHaveBeenLastCalledWith(expect.objectContaining({ error: result === 'foreign-review' ? 'operation_unconfirmed' : null }));
    expect(request.mock.calls.every(([, options]) => options.method === undefined)).toBe(true);
    client.stop();
  });
  it.each(['ready', 'dispatched', 'uncertain', 'stopped'])('coalesces a task-ready event during a browser read: %s', async outcome => {
    let publish, resolveRead, rejectRead;
    const value = { ...initial(), pending_operations: [{ ...record, action: 'auth.select', state: 'waiting' }] };
    const openBrowser = vi.fn().mockImplementationOnce(() => new Promise((resolve, reject) => { resolveRead = resolve; rejectRead = reject; }))
      .mockResolvedValue(true);
    const client = new DesktopSetupClient({ generation, request: async () => response(session), openBrowser,
      receive: vi.fn(), storage: storage(), stream: async (_request, _headers, _signal, listener) => {
        publish = next => listener(JSON.stringify(next)); publish(value);
      } });
    await client.connect(); client.remember(operationId); client.launchOperation = operationId;
    const reading = client.launchBrowser();
    publish({ ...value, revision: 1 }); publish({ ...value, revision: 2 });
    expect(openBrowser).toHaveBeenCalledOnce();
    if (outcome === 'stopped') client.stop();
    if (outcome === 'uncertain') rejectRead(Error('browser_unavailable'));
    else resolveRead(outcome === 'dispatched');
    await reading; await Promise.resolve();
    expect(openBrowser).toHaveBeenCalledTimes(outcome === 'ready' ? 2 : 1);
    expect(client.launchOperation).toBeNull();
    client.stop();
  });
  describe('native browser return ownership', () => {
    it.each(['withdrawn', 'original-receipt', 'original-reappears', 'same-child-new-attempt', 'new-parent'])(
      'discards an old return intent after a successor is observed: %s', async outcome => {
        let publish;
        const parent = '33333333-3333-4333-8333-333333333333', successor = '55555555-5555-4555-8555-555555555555';
        const authentication = { operation_id: parent, browser_operation_id: operationId, state: 'selected',
          choices: { google: true, 'microsoft-personal': true, telegram: false, tiktok: false, default: true },
          authentication_attempt: { authentication_attempt_id: '0199b234-5678-7000-8000-000000000002', provider: 'default', mode: 'sign-in' },
          actions: { select: false, cancel: true, open_browser: true, switch_account: false } };
        const value = { ...initial(), authentication, pending_operations: [{ ...record, action: 'auth.select', state: 'waiting' }] };
        const focusWindow = vi.fn(async () => {});
        const client = new DesktopSetupClient({ generation, focusWindow, now: () => 1000, receive: vi.fn(), storage: storage(),
          request: async path => response(path.endsWith('/session') ? session : { ...record, action: 'auth.select', state: 'completed' }),
          openBrowser: async () => true, stream: async (_request, _headers, _signal, listener) => { publish = next => listener(JSON.stringify(next)); publish(value); } });
        await client.connect(); client.remember(operationId); client.launchOperation = operationId; await client.launchBrowser();
        publish({ ...value, revision: 1, authentication: { ...authentication,
          operation_id: outcome === 'new-parent' ? successor : parent,
          browser_operation_id: ['same-child-new-attempt', 'new-parent'].includes(outcome) ? operationId : successor,
          authentication_attempt: { ...authentication.authentication_attempt,
            authentication_attempt_id: outcome === 'new-parent' ? authentication.authentication_attempt.authentication_attempt_id : '0199b234-5678-7000-8000-000000000003' } } });
        expect(client.focusIntent).toBeNull();
        if (outcome === 'original-receipt') await client.receipt();
        else if (outcome === 'original-reappears') publish({ ...value, revision: 2, authentication: { ...authentication, state: 'admitted' } });
        else publish({ ...value, revision: 2, authentication: { ...authentication, state: 'selection-required',
          browser_operation_id: null, authentication_attempt: null,
          actions: { select: true, cancel: false, open_browser: false, switch_account: false } } });
        expect(focusWindow).not.toHaveBeenCalled();
        client.stop();
      });
    it.each(['same-task', 'foreign-task', 'changed-known-journey'])(
      'binds the first journey from its exact browser task after the snapshot arrives: %s', async outcome => {
        let publish, release;
        const journey = '44444444-4444-4444-8444-444444444444', foreign = '55555555-5555-4555-8555-555555555555';
        const gate = new Promise(resolve => { release = resolve; });
        const value = { ...initial(), journey_id: outcome === 'changed-known-journey' ? foreign : null, pending_operations: [record] };
        const focusWindow = vi.fn(async () => {});
        const client = new DesktopSetupClient({ generation, focusWindow, now: () => 1000, receive: vi.fn(), storage: storage(),
          openBrowser: async owner => { await gate; owner.browserTaskPrepared({ operation_id: operationId,
            journey_id: outcome === 'foreign-task' ? foreign : journey, expires_at: 2000000000 }); return true; },
          request: async path => response(path.endsWith('/session') ? session : { ...record, state: 'completed' }),
          stream: async (_request, _headers, _signal, listener) => { publish = next => listener(JSON.stringify(next)); publish(value); } });
        await client.connect(); client.remember(operationId); client.launchOperation = operationId;
        const opening = client.launchBrowser();
        publish({ ...value, revision: 1, journey_id: journey });
        expect(focusWindow).not.toHaveBeenCalled();
        release(); await opening; await client.receipt();
        expect(focusWindow).toHaveBeenCalledTimes(outcome === 'same-task' ? 1 : 0);
        client.stop();
      });
    it.each(['admitted', 'receipt', 'unknown', 'foreign-child', 'foreign-journey', 'expired', 'not-opened', 'stopped', 'focus-denied', 'stale-revision'])(
      'returns focus only for a confirmed current native launch: %s', async outcome => {
        let publish, now = 1000;
        const parent = '33333333-3333-4333-8333-333333333333', journey = '44444444-4444-4444-8444-444444444444';
        const value = { ...initial(), revision: 2, journey_id: journey, next_task: 'account-connection', pending_operations: [{ ...record, action: 'auth.select', state: 'waiting' }],
          authentication: { operation_id: parent, browser_operation_id: operationId, state: 'selected',
            choices: { google: true, 'microsoft-personal': true, telegram: false, tiktok: false, default: true },
            authentication_attempt: { authentication_attempt_id: '0199b234-5678-7000-8000-000000000002', provider: 'default', mode: 'sign-in' },
            actions: { select: false, cancel: true, open_browser: true, switch_account: false } } };
        const focusWindow = outcome === 'focus-denied' ? vi.fn(async () => { throw Error('window_focus_deferred'); }) : vi.fn(async () => {});
        const client = new DesktopSetupClient({ generation, focusWindow, now: () => now,
          request: async path => response(path.endsWith('/session') ? session : { ...record, action: 'auth.select', state: 'completed' }),
          openBrowser: async () => outcome !== 'not-opened', receive: vi.fn(), storage: storage(),
          stream: async (_request, _headers, _signal, listener) => { publish = next => listener(JSON.stringify(next)); publish(value); } });
        await client.connect(); client.remember(operationId); client.launchOperation = operationId;
        await client.launchBrowser();
        expect(focusWindow).not.toHaveBeenCalled();
        if (outcome === 'expired') now += 180000;
        if (outcome === 'stopped') client.stop();
        if (outcome === 'receipt') await client.receipt();
        else if (outcome === 'stale-revision') expect(() => publish({ ...value, revision: 1,
          authentication: { ...value.authentication, state: 'admitted' } })).toThrow('invalid_snapshot');
        else publish({ ...value, revision: 3, journey_id: outcome === 'foreign-journey' ? '55555555-5555-4555-8555-555555555555' : journey,
          authentication: { ...value.authentication, state: outcome === 'unknown' ? 'unavailable' : 'admitted',
            browser_operation_id: outcome === 'foreign-child' ? parent : operationId } });
        await Promise.resolve();
        expect(focusWindow).toHaveBeenCalledTimes(['admitted', 'receipt', 'focus-denied'].includes(outcome) ? 1 : 0);
        publish({ ...value, revision: 4 });
        expect(focusWindow).toHaveBeenCalledTimes(['admitted', 'receipt', 'focus-denied'].includes(outcome) ? 1 : 0);
        client.stop();
      });
    it.each(['withdrawn', 'retired-receipt', 'expired-receipt', 'unconfirmed-receipt', 'unavailable', 'missing-parent', 'successor'])(
      'returns from a closed sign-in only on exact authoritative withdrawal: %s', async outcome => {
        let publish;
        const parent = '33333333-3333-4333-8333-333333333333';
        const authentication = { operation_id: parent, browser_operation_id: operationId, state: 'selected',
          choices: { google: true, 'microsoft-personal': true, telegram: false, tiktok: false, default: true },
          authentication_attempt: { authentication_attempt_id: '0199b234-5678-7000-8000-000000000002', provider: 'default', mode: 'sign-in' },
          actions: { select: false, cancel: true, open_browser: true, switch_account: false } };
        const value = { ...initial(), authentication, pending_operations: [{ ...record, action: 'auth.select', state: 'waiting' }] };
        const focusWindow = vi.fn(async () => {}), receive = vi.fn();
        const client = new DesktopSetupClient({ generation, focusWindow, now: () => 1000, receive, storage: storage(),
          request: async path => response(path.endsWith('/session') ? session : { ...record, action: 'auth.select', state: 'refused',
            reason: outcome === 'retired-receipt' ? 'none' : outcome === 'expired-receipt' ? 'operation_expired' : 'operation_unconfirmed' }),
          openBrowser: async () => true, stream: async (_request, _headers, _signal, listener) => { publish = next => listener(JSON.stringify(next)); publish(value); } });
        await client.connect(); client.remember(operationId); client.launchOperation = operationId; await client.launchBrowser();
        if (outcome.endsWith('receipt')) await client.receipt();
        else publish({ ...value, revision: 1, authentication: { ...authentication,
          operation_id: outcome === 'missing-parent' ? null : parent,
          browser_operation_id: outcome === 'successor' ? parent : null, state: outcome === 'unavailable' ? 'unavailable' : 'selection-required',
          authentication_attempt: outcome === 'successor' ? { ...authentication.authentication_attempt, authentication_attempt_id: '0199b234-5678-7000-8000-000000000003' } : null,
          actions: { select: true, cancel: false, open_browser: false, switch_account: false } } });
        expect(focusWindow).toHaveBeenCalledTimes(['withdrawn', 'retired-receipt', 'expired-receipt'].includes(outcome) ? 1 : 0);
        expect(receive.mock.calls.every(([state]) => !state.snapshot?.local_ready)).toBe(true);
        client.stop();
      });
  });
  it('keeps asynchronous browser failure available until explicit retry or the same admission supersedes it', async () => {
    let publish;
    const authentication = { operation_id: '33333333-3333-4333-8333-333333333333', browser_operation_id: operationId, state: 'selected',
      choices: { google: true, 'microsoft-personal': true, telegram: false, tiktok: false, default: true },
      authentication_attempt: { authentication_attempt_id: '0199b234-5678-7000-8000-000000000002', provider: 'default', mode: 'sign-in' },
      actions: { select: false, cancel: true, open_browser: true, switch_account: false } };
    const value = { ...initial(), authentication, pending_operations: [{ ...record, action: 'auth.select', state: 'waiting' }] };
    const receive = vi.fn(), openBrowser = vi.fn().mockRejectedValueOnce(Error('browser_unavailable')).mockResolvedValue(true);
    const client = new DesktopSetupClient({ generation, request: async () => response(session), openBrowser, receive, storage: storage(),
      stream: async (_request, _headers, _signal, listener) => { publish = next => listener(JSON.stringify(next)); publish(value); } });
    await client.connect(); client.remember(operationId); client.launchOperation = operationId; await client.launchBrowser();
    publish({ ...value, revision: 1 });
    expect(receive).toHaveBeenLastCalledWith(expect.objectContaining({ error: 'browser_unavailable' }));
    expect(openBrowser).toHaveBeenCalledOnce();
    await client.launchBrowser(true, operationId);
    expect(receive).toHaveBeenLastCalledWith(expect.objectContaining({ error: null }));
    expect(openBrowser).toHaveBeenCalledTimes(2);
    client.stop();
  });
  it('refuses implicit browser recovery instead of selecting an unrelated pending operation', async () => {
    const openBrowser = vi.fn();
    const client = new DesktopSetupClient({ generation, storage: storage(), receive: vi.fn(), openBrowser });
    client.connected = true; client.value = { ...initial(), pending_operations: [record] }; client.pending = operationId;
    await expect(client.launchBrowser(true)).rejects.toThrow('browser_unavailable');
    expect(openBrowser).not.toHaveBeenCalled();
    client.stop();
  });
  it.each(['completed', 'replacement', 'changed-journey', 'withdrawn-authentication'])(
    'retires explicit browser recovery before opening a successor task: %s', async outcome => {
      let publish, release;
      const otherId = '33333333-3333-4333-8333-333333333333';
      const authentication = { operation_id: otherId, browser_operation_id: operationId, state: 'selected',
        choices: { google: true, 'microsoft-personal': true, telegram: false, tiktok: false, default: true },
        authentication_attempt: { authentication_attempt_id: '0199b234-5678-7000-8000-000000000002', provider: 'default', mode: 'sign-in' },
        actions: { select: false, cancel: true, open_browser: true, switch_account: false } };
      const auth = outcome === 'withdrawn-authentication';
      const operation = { ...record, action: auth ? 'auth.select' : 'browser.access', state: 'waiting' };
      const value = { ...initial(), next_task: auth ? 'account-connection' : 'browser-access',
        pending_operations: [operation], ...(auth ? { authentication } : {}) };
      const invoke = vi.fn();
      const openBrowser = vi.fn(async (_client, _operation, _invoke, current) => {
        await new Promise(resolve => { release = resolve; });
        if (!current()) return false;
        invoke(); return true;
      });
      const client = new DesktopSetupClient({ generation, storage: storage(), receive: vi.fn(),
        openBrowser, request: async () => response(session),
        stream: async (_request, _headers, _signal, listener) => { publish = next => listener(JSON.stringify(next)); publish(value); } });
      await client.connect();
      const opening = client.launchBrowser(true, operationId);
      expect(openBrowser).toHaveBeenCalledOnce();
      const next = { ...value, revision: 1 };
      if (outcome === 'completed') next.pending_operations = [];
      if (outcome === 'replacement') next.pending_operations = [{ ...operation, operation_id: otherId }];
      if (outcome === 'changed-journey') next.journey_id = otherId;
      if (auth) next.authentication = { ...authentication, actions: { ...authentication.actions, open_browser: false } };
      publish(next); release(); await opening;
      expect(invoke).not.toHaveBeenCalled();
      expect(openBrowser).toHaveBeenCalledOnce();
      client.stop();
    });
  it.each(['google', 'microsoft-personal', 'default'])('retains the first %s click through preparation without selecting a replacement operation', async provider => {
    let publish, release;
    const authentication = { operation_id: null, browser_operation_id: null, state: 'selection-required',
      choices: { google: true, 'microsoft-personal': true, telegram: false, tiktok: false, default: true }, authentication_attempt: null,
      actions: { select: true, cancel: false, open_browser: false, switch_account: false } };
    const value = { ...initial(), next_task: 'account-connection', allowed_actions: ['account.connect'], authentication };
    const accepted = { ...record, action: 'account.connect', state: 'waiting' };
    let ready = false;
    const openBrowser = vi.fn(async (_client, selected) => { expect(selected).toBe(operationId); return ready; });
    const request = vi.fn(async path => path.endsWith('/session') ? response(session)
      : path.endsWith('/commands') ? new Promise(resolve => { release = () => resolve(response(accepted)); }) : response(accepted));
    const client = new DesktopSetupClient({ generation, request, storage: storage(), uuid: () => operationId, openBrowser, receive: vi.fn(),
      stream: async (_request, _headers, _signal, receive) => { publish = next => receive(JSON.stringify(next)); publish(value); } });
    await client.connect();
    const pending = client.act('account.connect', { provider });
    const preparing = { ...value, revision: 1, pending_operations: [accepted],
      authentication: { ...authentication, operation_id: operationId }, allowed_actions: ['auth.select'] };
    publish(preparing);
    await client.act('auth.select', { provider: 'default' });
    expect(client.pending).toBe(operationId);
    expect(client.launchOperation).toBe(operationId);
    release(); await pending;
    ready = true;
    publish({ ...preparing, revision: 2, authentication: { ...preparing.authentication, state: 'selected', browser_operation_id: operationId,
      authentication_attempt: { authentication_attempt_id: '0199b234-5678-7000-8000-000000000002', provider, mode: 'sign-in' },
      actions: { select: false, cancel: true, open_browser: true, switch_account: false } } });
    await Promise.resolve();
    expect(client.launchOperation).toBeNull();
    const calls = openBrowser.mock.calls.length;
    publish({ ...client.value, revision: 3 });
    expect(openBrowser).toHaveBeenCalledTimes(calls);
    expect(request.mock.calls.filter(([path]) => path.endsWith('/commands'))).toHaveLength(1);
    client.stop();
  });
  it.each(['stop', 'reconnect', 'unknown', 'refused'])('discards native launch intent after %s', async reason => {
    const openBrowser = vi.fn();
    const request = vi.fn(async path => response(path.endsWith('/session') ? session
      : { ...record, state: reason === 'unknown' ? 'outcome_unknown' : 'refused' }));
    const client = new DesktopSetupClient({ generation, request, storage: storage(), receive: vi.fn(), openBrowser,
      stream: async (_request, _headers, _signal, publish) => publish(JSON.stringify(initial())) });
    await client.connect(); client.remember(operationId); client.launchOperation = operationId;
    if (reason === 'stop') client.stop();
    else if (reason === 'reconnect') await client.connect();
    else await client.receipt();
    expect(client.launchOperation).toBeNull();
    expect(openBrowser).not.toHaveBeenCalled();
    client.stop();
  });
  it('a failed old receipt cannot clear a replacement authentication intent', async () => {
    let reject;
    const request = vi.fn(() => new Promise((_resolve, fail) => { reject = fail; }));
    const client = new DesktopSetupClient({ generation, request, storage: storage(), receive: vi.fn() });
    client.connected = true; client.session = session; client.remember(operationId); client.launchOperation = operationId;
    const pending = client.receipt();
    const replacement = '33333333-3333-4333-8333-333333333333';
    client.remember(replacement); client.launchOperation = replacement; client.launchEpoch++;
    reject(Error('lost response')); await pending;
    expect(client.pending).toBe(replacement);
    expect(client.launchOperation).toBe(replacement);
    expect(client.receive).not.toHaveBeenCalled();
    client.stop();
  });
  it.each(['auth.cancel', 'auth.switch'])('dispatches %s only against the current pending authentication', async action => {
    const parent = '33333333-3333-4333-8333-333333333333';
    const replacement = '44444444-4444-4444-8444-444444444444';
    const request = vi.fn(async () => response({ ...record, operation_id: replacement, action }));
    const client = new DesktopSetupClient({ generation, request, storage: storage(), uuid: () => replacement, receive: vi.fn() });
    client.connected = true;
    client.session = session;
    client.value = { ...initial(), allowed_actions: [action], authentication: { operation_id: parent,
      browser_operation_id: operationId }, pending_operations: [{ ...record, action: 'auth.select' }] };
    client.remember('55555555-5555-4555-8555-555555555555');
    await client.act(action, action === 'auth.switch' ? { provider: 'default' } : {});
    expect(request).not.toHaveBeenCalled();
    client.remember(operationId);
    await client.act(action, action === 'auth.switch' ? { provider: 'default' } : {});
    expect(request.mock.calls.filter(([path]) => path.endsWith('/commands'))).toHaveLength(1);
    const body = JSON.parse(request.mock.calls.find(([path]) => path.endsWith('/commands'))[1].body);
    expect(body.action).toBe(action);
    expect(body.provider).toBe(action === 'auth.switch' ? 'default' : undefined);
    await client.act(action, action === 'auth.switch' ? { provider: 'google' } : {});
    expect(request.mock.calls.filter(([path]) => path.endsWith('/commands'))).toHaveLength(1);
  });
  it('reconnects after a local service interruption without resending commands or opening Chrome', async () => {
    vi.useFakeTimers();
    const request = vi.fn().mockRejectedValueOnce(new TypeError('unavailable')).mockResolvedValue(response(session));
    const openBrowser = vi.fn(), receive = vi.fn();
    const client = new DesktopSetupClient({ generation, request, storage: storage(), openBrowser, receive,
      stream: async (_request, _headers, _signal, publish) => publish(JSON.stringify(initial())) });
    try {
      await client.connect();
      expect(client.connected).toBe(false);
      await vi.advanceTimersByTimeAsync(1000);
      expect(client.connected).toBe(true);
      expect(receive).toHaveBeenLastCalledWith(expect.objectContaining({ error: null, connected: true }));
      expect(request.mock.calls.every(([path, options]) => path.endsWith('/session') && !options.method)).toBe(true);
      expect(request).toHaveBeenCalledTimes(2);
      expect(openBrowser).not.toHaveBeenCalled();
    } finally { client.stop(); }
  });
  it('backs off unavailable reads and stops reconnecting when the presentation closes', async () => {
    vi.useFakeTimers();
    const request = vi.fn().mockRejectedValue(new TypeError('unavailable'));
    const client = new DesktopSetupClient({ generation, request, storage: storage(), receive: vi.fn() });
    await client.connect();
    await vi.advanceTimersByTimeAsync(2999);
    expect(request).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(request).toHaveBeenCalledTimes(3);
    client.stop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(request).toHaveBeenCalledTimes(3);
  });
  it('does not retry a revoked presentation as a service interruption', async () => {
    vi.useFakeTimers();
    const request = vi.fn(async () => response({}, 401));
    const client = new DesktopSetupClient({ generation, request, storage: storage(), receive: vi.fn() });
    await client.connect();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(request).toHaveBeenCalledOnce();
    client.stop();
  });
  it('recovers transient presentation refresh unavailability through bounded reconnect, exhausts to R13 without controls, and wakes once on focus', async () => {
    vi.useFakeTimers();
    const nextGeneration = '33333333-3333-4333-8333-333333333333';
    let refreshCalls = 0, releaseAttempt = null, dropStream = null, shouldFailAfterSuccess = false;
    const refreshPresentation = vi.fn(async () => {
      const attempt = ++refreshCalls;
      if (releaseAttempt) {
        const wait = releaseAttempt;
        releaseAttempt = null;
        await wait;
      }
      if (attempt <= 10 || shouldFailAfterSuccess) throw 'handoff_unavailable';
      return 'B'.repeat(43);
    });
    const request = vi.fn(async path => {
      if (path.endsWith('/session')) return response({}, 401);
      if (path.endsWith('/entry/consume')) return response({ ...session, generation: nextGeneration, journey_id: null });
      return response(record);
    });
    const received = [];
    const client = new DesktopSetupClient({ generation, request, refreshPresentation, storage: storage(),
      receive: state => received.push(state),
      stream: async (_request, _headers, _signal, publish) => {
        publish(JSON.stringify({ ...initial(), generation: nextGeneration }));
        await new Promise((_resolve, reject) => { dropStream = reject; });
      } });
    try {
      await client.connect();
      expect(received.at(-1)).toMatchObject({ connected: false, error: 'status_unavailable' });
      // Initial attempt + 6 bounded retries (1s..30s = 61s) exhaust into R13.
      await vi.advanceTimersByTimeAsync(61_000);
      expect(refreshPresentation).toHaveBeenCalledTimes(7);
      expect(received.at(-1)).toMatchObject({ connected: false, error: 'connection_unavailable' });
      document.documentElement.innerHTML = markup;
      renderSetup(document, { snapshot: null, connected: false, error: 'connection_unavailable' });
      expect(document.querySelector('main').dataset.journeyState).toBe('desktop.setup_unavailable');
      expect(document.getElementById('task-status').textContent).toBe("Dipsy can't connect right now.");
      expect(document.getElementById('task-status').dataset.tone).toBe('error');
      expect(document.getElementById('task-reconnect')).toBeNull();
      expect([...document.querySelectorAll('button')].filter(button => !button.closest('[hidden]'))).toEqual([]);
      renderSetup(document, { snapshot: { ...initial(), journey_id: operationId }, connected: false, error: 'connection_unavailable' });
      expect(document.getElementById('task-status').textContent).toBe('Dipsy can’t connect right now. Your setup progress is saved.');
      expect([...document.querySelectorAll('button')].filter(button => !button.closest('[hidden]'))).toEqual([]);

      // After the budget, R13 stays active and reconnect repeats every 30s.
      await vi.advanceTimersByTimeAsync(29_999);
      expect(refreshPresentation).toHaveBeenCalledTimes(7);
      await vi.advanceTimersByTimeAsync(1);
      expect(refreshPresentation).toHaveBeenCalledTimes(8);
      expect(received.at(-1)).toMatchObject({ connected: false, error: 'connection_unavailable' });
      await vi.advanceTimersByTimeAsync(30_000);
      expect(refreshPresentation).toHaveBeenCalledTimes(9);
      expect(received.at(-1)).toMatchObject({ connected: false, error: 'connection_unavailable' });

      // A wake during the 30s wait cancels the timer, runs one attempt without overlapping, and reschedules 30s after failure.
      await vi.advanceTimersByTimeAsync(10_000);
      let finishWakeFailure;
      releaseAttempt = new Promise(resolve => { finishWakeFailure = resolve; });
      client.wake();
      await vi.advanceTimersByTimeAsync(1);
      expect(refreshPresentation).toHaveBeenCalledTimes(10);
      client.wake();
      await vi.advanceTimersByTimeAsync(30_000);
      expect(refreshPresentation).toHaveBeenCalledTimes(10);
      finishWakeFailure();
      await vi.advanceTimersByTimeAsync(0);
      expect(received.at(-1)).toMatchObject({ connected: false, error: 'connection_unavailable' });
      await vi.advanceTimersByTimeAsync(29_999);
      expect(refreshPresentation).toHaveBeenCalledTimes(10);

      // Slow attempt in flight never overlaps with a concurrent wake; success leaves R13 and resets to 1s delay.
      let finishSlowSuccess;
      releaseAttempt = new Promise(resolve => { finishSlowSuccess = resolve; });
      await vi.advanceTimersByTimeAsync(1);
      expect(refreshPresentation).toHaveBeenCalledTimes(11);
      client.wake();
      expect(refreshPresentation).toHaveBeenCalledTimes(11);
      finishSlowSuccess();
      await vi.advanceTimersByTimeAsync(1);
      expect(client.connected).toBe(true);
      expect(received.at(-1)).toMatchObject({ connected: true, error: null });

      shouldFailAfterSuccess = true;
      dropStream(new Error('stream_unavailable'));
      await vi.advanceTimersByTimeAsync(0);
      expect(received.at(-1)).toMatchObject({ connected: false, error: 'status_unavailable' });
      await vi.advanceTimersByTimeAsync(999);
      expect(refreshPresentation).toHaveBeenCalledTimes(11);
      await vi.advanceTimersByTimeAsync(1);
      expect(refreshPresentation).toHaveBeenCalledTimes(12);
    } finally { client.stop(); }
  });
  it('recovers transient entry consume server failures through bounded reconnect', async () => {
    vi.useFakeTimers();
    const nextGeneration = '33333333-3333-4333-8333-333333333333';
    let consumes = 0;
    const refreshPresentation = vi.fn(async () => 'B'.repeat(43));
    const request = vi.fn(async path => {
      if (path.endsWith('/session')) return response({}, 401);
      if (path.endsWith('/entry/consume')) {
        return ++consumes === 1 ? response({ detail: 'starting' }, 503)
          : response({ ...session, generation: nextGeneration, journey_id: null });
      }
      return response(record);
    });
    const received = [];
    const client = new DesktopSetupClient({ generation, request, refreshPresentation, storage: storage(),
      receive: state => received.push(state),
      stream: async (_request, _headers, _signal, publish) => publish(JSON.stringify({ ...initial(), generation: nextGeneration })) });
    try {
      await client.connect();
      expect(received.at(-1)).toMatchObject({ connected: false, error: 'status_unavailable' });
      await vi.advanceTimersByTimeAsync(1000);
      expect(client.connected).toBe(true);
      expect(refreshPresentation).toHaveBeenCalledTimes(2);
      expect(received.at(-1)).toMatchObject({ connected: true, error: null });
    } finally { client.stop(); }
  });
  it('treats terminal presentation refresh failures as R14 without reconnecting or offering Retry', async () => {
    vi.useFakeTimers();
    for (const failure of ['handoff_rejected', 'desktop_admission_required', 'invalid-code', 'consume-401', 'malformed-session']) {
      const refreshPresentation = vi.fn(async () => {
        if (failure === 'handoff_rejected' || failure === 'desktop_admission_required') throw failure;
        if (failure === 'invalid-code') return 'short';
        return 'B'.repeat(43);
      });
      const request = vi.fn(async path => {
        if (path.endsWith('/session')) {
          return failure === 'malformed-session' ? response({ role: 'other', generation }) : response({}, 401);
        }
        return response({ detail: 'expired' }, 401);
      });
      const received = [];
      const client = new DesktopSetupClient({ generation, request, refreshPresentation, storage: storage(),
        receive: state => received.push(state) });
      try {
        await client.connect();
        await vi.advanceTimersByTimeAsync(60_000);
        client.wake();
        await vi.advanceTimersByTimeAsync(60_000);
        expect(received.at(-1)).toMatchObject({ connected: false, error: 'session_unavailable' });
        expect(request.mock.calls.filter(([path]) => path.endsWith('/session'))).toHaveLength(1);
        document.documentElement.innerHTML = markup;
        renderSetup(document, { snapshot: initial(), connected: false, error: 'session_unavailable' });
        expect(document.querySelector('main').dataset.journeyState).toBe('desktop.setup_unavailable');
        expect(document.getElementById('task-title').textContent).toBe('Setup couldn’t continue');
        expect(document.getElementById('task-action').hidden).toBe(true);
        expect(document.getElementById('task-reconnect')).toBeNull();
        expect([...document.querySelectorAll('button')].filter(button => !button.closest('[hidden]'))).toEqual([]);
      } finally { client.stop(); }
    }
  });
  it.each(['/desktop/entry', '/browser-tasks/entry'])('retains a desktop locator only from its exact consumed native entry: %s', async path => {
    const local = { setItem: vi.fn() }, replace = vi.fn(), scrub = vi.fn();
    const request = vi.fn(async () => {
      expect(scrub).toHaveBeenCalledExactlyOnceWith(null, '', path);
      return { ok: true, json: async () => ({ generation }) };
    });
    await runInNewContext(entryScript, { URLSearchParams, location: { pathname: path, hash: '#code=' + 'A'.repeat(43), replace },
      history: { replaceState: scrub }, document: { getElementById: () => ({}) }, sessionStorage: local, fetch: request });
    expect(local.setItem).toHaveBeenCalledExactlyOnceWith(path === '/desktop/entry' ? 'desktop-presentation' : 'browser-task-launch', generation);
    expect(replace).toHaveBeenCalledExactlyOnceWith((path === '/desktop/entry' ? '/onboarding' : '/browser-tasks') + '#presentation=' + generation);
    expect(request).toHaveBeenCalledOnce();
  });
  it('reconciles only the selected pending operation and carries both runtime request contexts', async () => {
    const other = '33333333-3333-4333-8333-333333333333';
    const request = vi.fn(async path => response(path.endsWith('/session') ? { ...session, stage: 'runtime' } : record));
    const value = { ...initial(), pending_operations: [record, { ...record, operation_id: other, action: 'activation.review' }] };
    const client = new DesktopSetupClient({ generation, request, storage: storage(), receive: vi.fn(), operatorCsrf: () => 'operator-csrf',
      stream: async (_request, _headers, _signal, receive) => receive(JSON.stringify(value)) });
    await client.connect();
    await client.reconcile(other);
    expect(request).toHaveBeenLastCalledWith('/api/v1/desktop/setup/operations/' + other + '/reconcile', expect.objectContaining({
      method: 'POST', headers: expect.objectContaining({ 'X-Desktop-Generation': generation, 'X-Desktop-CSRF': session.csrf_token,
        'X-CSRF-Token': 'operator-csrf' }),
    }));
    await expect(client.reconcile('44444444-4444-4444-8444-444444444444')).rejects.toThrow('operation_unavailable');
    expect(request).toHaveBeenCalledTimes(2);
  });
  it('preserves the browser receiver when using native fetch', async () => {
    vi.stubGlobal('fetch', function () {
      expect(this).toBe(globalThis);
      return Promise.resolve(response(session));
    });
    const receive = vi.fn();
    const client = new DesktopSetupClient({ generation, storage: storage(), receive,
      stream: async (_request, _headers, _signal, receive) => receive(JSON.stringify(initial())) });
    await client.connect();
    expect(receive).toHaveBeenLastCalledWith(expect.objectContaining({ connected: true, error: null }));
  });
  it('validates with runtime code generation disabled by the page policy', () => {
    const exports = {};
    runInNewContext(generatedContract.replace(/^export const (\w+) =/gmu, 'exports.$1 ='),
      { exports }, { codeGeneration: { strings: false, wasm: false } });
    expect(exports.validateDocument(initial())).toBe(true);
    expect(exports.validateDocument({ ...initial(), surprise: true })).toBe(false);
    expect(exports.validateOperation(record)).toBe(true);
  });
  it('validates the closed coordinator snapshot and exact presentation', () => {
    expect(snapshot(JSON.stringify(initial()), generation).next_task).toBe('browser-access');
    expect(() => snapshot(JSON.stringify({ ...initial(), surprise: true }), generation)).toThrow();
    expect(() => snapshot(JSON.stringify(initial()), operationId)).toThrow();
    expect(() => snapshot(JSON.stringify(initial()).replace('"revision":0', '"revision":0,"revision":1'), generation)).toThrow();
    expect(() => snapshot(JSON.stringify({ ...initial(), revision: Number.MAX_SAFE_INTEGER + 1 }), generation)).toThrow();
  });

  it.each(['impossible', 'extra', 'unauthorized', 'old-profile'])('refuses invalid readiness disclosure: %s', kind => {
    const value = initial();
    value.analysis_readiness = { availability: 'current', creator_account_id: 'creator-account', value: {
      schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission: 'admitted' } };
    if (kind === 'impossible') value.analysis_readiness.value.commercial_authority = 'required';
    if (kind === 'extra') value.analysis_readiness.value.license_id = 'protected';
    if (kind === 'unauthorized') value.analysis_readiness.availability = 'unauthorized';
    if (kind === 'old-profile') value.profile = 'desktop-onboarding.v1';
    expect(() => snapshot(JSON.stringify(value), generation)).toThrow('invalid_snapshot');
  });

  it('bounds the shared read through response-body settlement and does not create work', async () => {
    vi.useFakeTimers();
    const request = vi.fn(async () => ({ ok: true, text: () => new Promise(() => {}) }));
    const client = new DesktopSetupClient({ generation, request, receive: vi.fn(), storage: storage() });
    const outcome = expect(client.json('/api/v1/desktop/setup')).rejects.toThrow('request_unconfirmed');
    await vi.advanceTimersByTimeAsync(3000);
    await outcome;
    expect(request).toHaveBeenCalledOnce();
    expect(request.mock.calls[0][1].signal.aborted).toBe(true);
    expect(request.mock.calls[0][1].method).toBeUndefined();
    expect(client.pending).toBeNull();
    client.stop();
  });

  it('reads setup without issuing commands and coalesces duplicate clicks', async () => {
    let release;
    const request = vi.fn(async (path, options) => {
      if (path.endsWith('/session')) return response(session);
      if (path.endsWith('/timing')) return new Response(null, { status: 204 });
      if (path.endsWith('/commands')) return new Promise(resolve => { release = () => resolve(response(record)); });
      expect(options.method).toBeUndefined();
      return response(record);
    });
    const client = new DesktopSetupClient({ generation, request, storage: storage(), uuid: () => operationId,
      receive: vi.fn(), stream: async (_request, _headers, _signal, receive) => receive(JSON.stringify(initial())) });
    await client.connect();
    expect(request.mock.calls).toHaveLength(1);
    const first = client.act('browser.access');
    await client.act('browser.access');
    release();
    await first;
    const mutations = request.mock.calls.filter(([path]) => path.endsWith('/commands'));
    expect(mutations).toHaveLength(1);
    expect(JSON.parse(mutations[0][1].body).operation_id).toBe(operationId);
    expect(client.pending).toBe(operationId);
  });

  it('retains a lost response across reload and resolves only the original receipt', async () => {
    const saved = storage();
    let committed = false;
    const request = vi.fn(async path => {
      if (path.endsWith('/session')) return response(session);
      if (path.endsWith('/commands')) throw new Error('connection lost');
      return response({ ...record, state: committed ? 'completed' : 'outcome_unknown' });
    });
    const options = { generation, request, storage: saved, uuid: () => operationId, receive: vi.fn(),
      stream: async (_request, _headers, _signal, receive) => receive(JSON.stringify(initial())) };
    const first = new DesktopSetupClient(options);
    await first.connect();
    await first.act('browser.access');
    const reopened = new DesktopSetupClient(options);
    await reopened.connect();
    await reopened.act('browser.access');
    expect(reopened.pending).toBe(operationId);
    committed = true;
    await reopened.receipt();
    expect(reopened.pending).toBeNull();
    expect(request.mock.calls.filter(([path]) => path.endsWith('/commands'))).toHaveLength(1);
  });

  it('a closed admission refusal does not leave a nonexistent operation pending', async () => {
    const request = vi.fn(async path => path.endsWith('/session') ? response(session) : response({ reason: 'setup_changed' }, 409));
    const client = new DesktopSetupClient({ generation, request, storage: storage(), uuid: () => operationId, receive: vi.fn(),
      stream: async (_request, _headers, _signal, receive) => receive(JSON.stringify(initial())) });
    await client.connect();
    await client.act('browser.access');
    expect(client.pending).toBeNull();
  });
});

describe('desktop setup presentation', () => {
  const mount = () => { document.documentElement.innerHTML = markup; };
  const signIn = () => ({ operation_id: null, browser_operation_id: null, state: 'selection-required',
    choices: { google: true, 'microsoft-personal': true, telegram: false, tiktok: false, default: true },
    actions: { select: true, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null });

  it('moves the browser launch gesture into one pending slot, then exposes only the owned launch', () => {
    mount();
    const get = id => document.getElementById(id);
    const snapshot = initial();
    renderSetup(document, { snapshot, connected: true });
    expect(get('task-step').textContent).toBe('Step 1 of 3 · Set up browser access');
    expect(get('task-title').textContent).toBe('Bring in your conversations');
    expect(get('task-action').textContent).toBe('Continue in browser');
    expect(get('task-action').dataset.action).toBe('browser.access');
    expect(get('browser-reopen').hidden).toBe(true);
    renderSetup(document, { snapshot, connected: true, acting: 'browser.access' });
    expect(get('task-action').hidden).toBe(true);
    expect(get('setup-code-open').hidden).toBe(true);
    expect(get('task-pending').textContent).toBe('Getting the browser ready…');
    const opened = { ...snapshot, allowed_actions: [], pending_operations: [record] };
    renderSetup(document, { snapshot: opened, connected: true });
    expect(get('task-pending').hidden).toBe(true);
    expect(get('browser-reopen').dataset.operation).toBe(operationId);
    expect(get('task-description').textContent).toBe('Finish setup in your browser. This window updates by itself.');
    renderSetup(document, { snapshot: opened, connected: true, error: 'browser_needs_permission' });
    expect(get('task-status').textContent).toBe('Allow access to the creator site in the extension.');
    expect(get('browser-reopen').compareDocumentPosition(get('task-status')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    renderSetup(document, { snapshot: opened, connected: false, error: 'status_unavailable' });
    expect(get('browser-reopen').hidden).toBe(true);
    expect(get('task-pending').textContent).toBe('Reconnecting…');
    renderSetup(document, { snapshot: { ...opened, next_task: 'account-connection', pending_operations: [] }, connected: true });
    expect(get('task-step').textContent).toBe('Step 2 of 3 · Connect your account');
    expect(get('task-action').hidden).toBe(true);
  });

  it('shows provider proof only from the current admitted confirmation, including a fresh mount', () => {
    const makeSnapshot = provider => ({ ...initial(), next_task: 'account-connection',
      allowed_actions: ['account.confirm', 'auth.switch'], pending_operations: [{ ...record, action: 'account.connect' }],
      decision: { operation_id: operationId, action: 'connect-account', creator_id: 'expected',
        computer_name: 'Test computer', expires_at: '2030-01-01T00:00:00Z' },
      decision_status: { operation_id: operationId, state: 'confirmation-required' },
      authentication: { ...signIn(), state: 'admitted', operation_id: operationId,
        actions: { ...signIn().actions, select: false, switch_account: true },
        authentication_attempt: provider && { authentication_attempt_id: generation, provider, mode: 'sign-in' } },
    });
    const proof = () => document.getElementById('decision-provider');
    for (const provider of ['google', 'microsoft-personal', 'telegram', 'tiktok']) {
      mount();
      renderSetup(document, { snapshot: makeSnapshot(provider), connected: true });
      expect(proof().textContent).toBe('Sign-in confirmed');
      expect(proof().hidden).toBe(false);
      expect(document.getElementById('sign-in-switch').textContent).toBe('Change sign-in');
      expect(document.getElementById('task-description').textContent).toBe('Check the creator account and this computer, then connect them.');
    }
    const current = makeSnapshot('google');
    for (const state of [
      { snapshot: makeSnapshot(null), provider: 'google' },
      { snapshot: makeSnapshot('default') },
      { snapshot: { ...current, authentication: { ...current.authentication, operation_id: generation } } },
      { snapshot: { ...current, authentication: { ...current.authentication, state: 'selected' } } },
      { snapshot: { ...current, decision_status: { operation_id: generation, state: 'confirmation-required' } } },
      { snapshot: current, choosingAccount: true },
      { snapshot: current, acting: 'auth.switch' },
      { snapshot: current, continuingProvider: 'telegram' },
      { snapshot: { ...current, pending_operations: [{ ...record, action: 'auth.switch' }] } },
    ]) {
      renderSetup(document, { connected: true, ...state });
      expect(proof().hidden).toBe(true);
      expect(proof().textContent).toBe('');
    }
  });

  it('names only a verified expected account in mismatch copy', () => {
    mount();
    const get = id => document.getElementById(id);
    const value = { ...initial(), allowed_actions: [], reason: 'account_mismatch',
      pending_operations: [{ ...record, reason: 'account_mismatch' }] };
    const label = { creator_id: 'expected', display_name: 'Expected studio', username: null };
    renderSetup(document, { connected: true, snapshot: { ...value,
      facts: { ...value.facts, account: 'verified' }, creator_label: label } });
    expect(get('task-status').textContent).toBe('You’re signed in to a different creator account. Switch to Expected studio in your browser to continue.');
    renderSetup(document, { connected: true, error: 'account_mismatch', snapshot: { ...value,
      reason: 'none', creator_label: { ...label, creator_id: 'observed', display_name: 'Wrong studio' } } });
    expect(get('task-status').textContent).toBe('Switch to the creator account you want to connect in your browser.');
    expect(get('header-account').hidden).toBe(true);
    expect(document.querySelector('main').textContent).not.toContain('Wrong studio');
    renderSetup(document, { connected: true, snapshot: { ...value, next_task: 'account-connection',
      facts: { ...value.facts, account: 'verified' }, creator_label: { ...label, creator_id: 'observed', display_name: 'Wrong studio' },
      decision: { operation_id: operationId, action: 'connect-account', creator_id: 'expected', computer_name: 'Test computer' } } });
    expect(get('task-status').textContent).toBe('Switch to the creator account you want to connect in your browser.');
    expect(document.querySelector('main').textContent).not.toContain('Wrong studio');
  });

  it('uses the passkey attempt outcome and swaps pairing only after owner acceptance', () => {
    mount();
    const get = id => document.getElementById(id);
    const snapshot = { ...initial(), next_task: 'finish-setup', allowed_actions: ['browser.pair'] };
    const authentication = { checked: true, authenticated: false, enrolled: false, busy: false, error: 'passkey_unconfirmed' };
    renderSetup(document, { snapshot, connected: true, authentication: { ...authentication, attempt: 'create' } });
    expect(get('task-title').textContent).toBe('Protect your analytics on this computer');
    expect(get('local-auth-status').textContent).toBe('Passkey setup didn’t finish.');
    expect(get('local-authenticate').textContent).toBe('Create passkey');
    renderSetup(document, { snapshot, connected: true, authentication: { ...authentication, enrolled: true, attempt: 'sign-in' } });
    expect(get('task-title').textContent).toBe('Confirm it’s you');
    expect(get('local-auth-status').textContent).toBe('Sign-in didn’t finish.');
    expect(get('local-authenticate').textContent).toBe('Sign in with passkey');
    const ready = { ...authentication, enrolled: true, authenticated: true, error: null };
    renderSetup(document, { snapshot, connected: true, authentication: ready, acting: 'browser.pair' });
    expect(get('task-action').hidden).toBe(false);
    expect(get('task-action').disabled).toBe(true);
    const accepted = { ...snapshot, allowed_actions: [], pending_operations: [{ ...record, action: 'browser.pair' }] };
    renderSetup(document, { snapshot: accepted, connected: true, authentication: ready, browserLaunchPending: true });
    expect(get('task-action').hidden).toBe(true);
    expect(get('task-step').textContent).toBe('Step 3 of 3 · Finish setup');
    expect(get('task-description').textContent).toBe('New messages show up in your analytics once it’s connected.');
    expect(get('task-pending').textContent).toBe('Keep the browser open. This window continues by itself.');
  });

  it('offers one click sign-in choices and plain unfinished copy after the popup ends', () => {
    mount();
    const value = { ...initial(), next_task: 'account-connection', allowed_actions: ['auth.select'],
      pending_operations: [{ ...record, action: 'auth.select', state: 'outcome_unknown', reason: 'operation_unconfirmed' }],
      authentication: { ...signIn(), operation_id: operationId, browser_operation_id: operationId,
        state: 'reauthentication-required' } };
    renderSetup(document, { snapshot: value, connected: true, pending: operationId, provider: 'google' });
    expect(document.getElementById('task-status').textContent).toBe('Sign-in did not finish.');
    expect(document.getElementById('sign-in-options').hidden).toBe(false);
    const button = document.querySelector('[data-provider="google"]');
    expect(button.disabled).toBe(false);
    expect(button.hasAttribute('aria-busy')).toBe(false);
    expect(button.dataset.action).toBe('auth.select');
    expect(document.getElementById('browser-reopen').hidden).toBe(true);
  });

  it('replaces only the ended sign-in attempt through the owner-admitted provider action', async () => {
    const nextId = '44444444-4444-4444-8444-444444444444';
    const value = { ...initial(), journey_id: generation, next_task: 'account-connection',
      allowed_actions: ['auth.select'], pending_operations: [{ ...record, action: 'auth.select', state: 'outcome_unknown', reason: 'operation_unconfirmed' }],
      authentication: { ...signIn(), operation_id: generation, browser_operation_id: operationId, state: 'reauthentication-required' } };
    const request = vi.fn(async (url) => response(url.endsWith('/commands')
      ? { ...record, operation_id: nextId, action: 'auth.select' }
      : { ...record, operation_id: nextId, action: 'auth.select', state: 'waiting' }));
    const client = new DesktopSetupClient({ generation, request, storage: storage(), uuid: () => nextId, receive: vi.fn() });
    client.value = value; client.session = session; client.connected = true;
    client.remember(generation);
    await client.act('auth.select', { provider: 'google' });
    expect(request.mock.calls.filter(([url]) => url.endsWith('/commands'))).toHaveLength(0);
    client.remember(operationId);
    await client.act('auth.select', { provider: 'google' });
    expect(request.mock.calls.filter(([url]) => url.endsWith('/commands'))).toHaveLength(1);
    expect(client.pending).toBe(nextId);
    client.stop();
  });


  it('offers explicit ceremony recovery without launching Chrome or choosing a provider', async () => {
    mount();
    const parent = generation;
    const value = { ...initial(), journey_id: parent, next_task: 'account-connection',
      allowed_actions: ['auth.select'], pending_operations: [{ ...record, operation_id: parent, action: 'account.connect', state: 'waiting' }],
      authentication: { ...signIn(), operation_id: parent, state: 'replacement-required',
        choices: { google: false, 'microsoft-personal': false, telegram: false, tiktok: false, default: false } } };
    renderSetup(document, { snapshot: value, connected: true, pending: parent });
    const button = document.getElementById('task-action');
    expect(button.hidden).toBe(false);
    expect(button.disabled).toBe(false);
    expect(button.textContent).toBe('Sign in again');
    expect(button.dataset.replaceCeremony).toBe('true');
    expect(document.getElementById('sign-in-options').hidden).toBe(true);
    expect(document.getElementById('browser-reopen').hidden).toBe(true);
    const request = vi.fn(async url => response(url.endsWith('/commands')
      ? { ...record, action: 'auth.select' }
      : { ...record, action: 'auth.select', state: 'completed' }));
    const openBrowser = vi.fn();
    const client = new DesktopSetupClient({ generation, request, openBrowser, storage: storage(), uuid: () => operationId, receive: vi.fn() });
    client.value = value; client.session = session; client.connected = true; client.remember(parent);
    await client.act('auth.select', { replace_ceremony: true });
    expect(openBrowser).not.toHaveBeenCalled();
    const sent = JSON.parse(request.mock.calls.find(([url]) => url.endsWith('/commands'))[1].body);
    expect(sent.replace_ceremony).toBe(true);
    expect(sent.provider).toBeUndefined();
    client.stop();
  });

  it('continues an expired source without opening Chrome, then admits a provider choice', async () => {
    mount();
    const parent = generation;
    const value = { ...initial(), journey_id: parent, next_task: 'account-connection',
      continuation_available: true, allowed_actions: ['account.connect'], authentication: null };
    renderSetup(document, { snapshot: value, connected: true });
    expect(document.getElementById('task-action').textContent).toBe('Continue setup');
    expect(document.getElementById('task-action').hidden).toBe(false);
    expect(document.getElementById('sign-in-options').hidden).toBe(true);
    const request = vi.fn(async () => response({ ...record, action: 'account.connect', state: 'accepted' }));
    const openBrowser = vi.fn(async () => true);
    const client = new DesktopSetupClient({ generation, request, openBrowser, storage: storage(), uuid: () => operationId, receive: vi.fn() });
    client.value = value; client.session = session; client.connected = true;
    await client.act('account.connect');
    expect(openBrowser).not.toHaveBeenCalled();
    const sent = JSON.parse(request.mock.calls.find(([url]) => url.endsWith('/commands'))[1].body);
    expect(sent.provider).toBeUndefined();
    const selection = { ...value, continuation_available: false, allowed_actions: ['auth.select'],
      pending_operations: [{ ...record, operation_id: parent, action: 'account.connect', state: 'waiting' }],
      authentication: { ...signIn(), operation_id: parent, state: 'selection-required' } };
    renderSetup(document, { snapshot: selection, connected: true, pending: parent });
    expect(document.getElementById('sign-in-options').hidden).toBe(false);
    client.value = selection; client.remember(parent);
    await client.act('auth.select', { provider: 'google' });
    expect(openBrowser).toHaveBeenCalledTimes(1);
    client.stop();
  });

  it('keeps interrupted ceremony recovery passive even after desktop restart', () => {
    mount();
    const value = { ...initial(), next_task: 'account-connection', allowed_actions: [],
      pending_operations: [{ ...record, action: 'auth.select', state: 'outcome_unknown', reason: 'operation_unconfirmed' }],
      authentication: { ...signIn(), operation_id: generation, state: 'replacement-pending',
        actions: { select: false, cancel: false, open_browser: false, switch_account: false },
        choices: { google: false, 'microsoft-personal': false, telegram: false, tiktok: false, default: false } } };
    renderSetup(document, { snapshot: value, connected: true });
    expect(document.getElementById('task-status').textContent).toBe('Checking sign-in\u2026');
    expect(document.getElementById('task-action').hidden).toBe(true);
    expect(document.getElementById('sign-in-options').hidden).toBe(true);
    expect(document.getElementById('browser-reopen').hidden).toBe(true);
  });

  it('does not suggest opening an unavailable browser and reserves the warning line', () => {
    mount();
    renderSetup(document, { snapshot: { ...initial(), pending_operations: [record], allowed_actions: [] },
      connected: true, error: 'chrome_unavailable' });
    expect(document.getElementById('browser-reopen').hidden).toBe(true);
    expect(document.getElementById('task-status').dataset.tone).toBe('warning');
    expect(document.getElementById('task-status').dataset.reservedRegion).toBe('desktop-task-status');
    renderSetup(document, { snapshot: { ...initial(), pending_operations: [record], allowed_actions: [] },
      connected: true, error: 'chrome_unavailable', chromeAvailable: true });
    expect(document.getElementById('browser-reopen').hidden).toBe(false);
    expect(document.getElementById('task-status').textContent).not.toContain('Install Chrome');
  });

  it('renders only configured provider choices with the governed initial action', () => {
    mount();
    const value = { ...initial(), next_task: 'account-connection', allowed_actions: ['account.connect'],
      authentication: { ...signIn(), choices: { google: true, 'microsoft-personal': false, telegram: false, tiktok: false, default: true } } };
    renderSetup(document, { snapshot: value, connected: true });
    expect(document.getElementById('sign-in-options').hidden).toBe(false);
    expect(document.querySelector('[data-provider="google"]').dataset.action).toBe('account.connect');
    expect(document.querySelector('[data-provider="google"]').disabled).toBe(false);
    expect(document.querySelector('[data-provider="microsoft-personal"]').hidden).toBe(true);
    expect(document.querySelector('[data-provider="default"]').hidden).toBe(false);
    expect(document.getElementById('task-action').hidden).toBe(true);
    renderSetup(document, { snapshot: value, connected: false });
    expect([...document.querySelectorAll('[data-provider]')].every(button => button.disabled)).toBe(true);
  });

  it('waits without redundant launch or cancel buttons and binds recovery to the current attempt', () => {
    mount();
    const value = { ...initial(), next_task: 'account-connection', allowed_actions: ['auth.switch', 'auth.cancel'],
      authentication: { ...signIn(), operation_id: operationId, browser_operation_id: operationId, state: 'selected',
        actions: { select: false, cancel: true, open_browser: true, switch_account: true } },
      pending_operations: [{ ...record, action: 'auth.select', state: 'waiting' }] };
    renderSetup(document, { snapshot: value, connected: true, pending: operationId });
    expect(document.getElementById('task-status').textContent).toBe('Finish signing in in your browser. This window continues by itself.');
    expect(document.getElementById('sign-in-cancel')).toBeNull();
    expect(document.getElementById('browser-reopen').hidden).toBe(true);
    expect(document.getElementById('sign-in-switch').hidden).toBe(true);
    renderSetup(document, { snapshot: value, connected: true, error: 'browser_unavailable' });
    expect(document.getElementById('browser-reopen').hidden).toBe(false);
    expect(document.getElementById('browser-reopen').textContent).toBe('Open browser');
    expect(document.getElementById('browser-reopen').dataset.operation).toBe(operationId);
    const reviewed = { ...value, decision: { operation_id: operationId, action: 'connect-account', creator_id: 'selected', computer_name: 'This computer', expires_at: '2030-01-01T00:00:00.000Z' }, authentication: { ...value.authentication, state: 'admitted' } };
    renderSetup(document, { snapshot: reviewed, connected: true, choosingAccount: true });
    expect(document.getElementById('sign-in-options').hidden).toBe(false);
    expect(document.querySelector('[data-provider="google"]').dataset.action).toBe('auth.switch');
    expect(document.getElementById('sign-in-back').hidden).toBe(false);
    renderSetup(document, { snapshot: reviewed, connected: true });
    expect(document.getElementById('browser-reopen').hidden).toBe(true);
    expect(document.getElementById('sign-in-switch').hidden).toBe(false);
    renderSetup(document, { snapshot: { ...value, decision_status: { operation_id: operationId, state: 'accepted' } }, connected: true });
    for (const id of ['browser-reopen', 'sign-in-switch', 'sign-in-options']) expect(document.getElementById(id).hidden).toBe(true);
  });

  it.each(['browser_needs_account', 'browser_needs_consent', 'browser_needs_permission'])(
    'replaces the Chrome wait with the %s error and a Try again', error => {
      mount();
      const value = { ...initial(), next_task: 'account-connection', allowed_actions: ['auth.switch', 'auth.cancel'],
        authentication: { ...signIn(), operation_id: operationId, browser_operation_id: operationId, state: 'selected',
          actions: { select: false, cancel: true, open_browser: true, switch_account: true } },
        pending_operations: [{ ...record, action: 'auth.select', state: 'waiting' }] };
      renderSetup(document, { snapshot: value, connected: true, error });
      const status = document.getElementById('task-status').textContent;
      expect(status).not.toContain('Finish signing in in your browser.');
      expect(status.length).toBeGreaterThan(10);
      expect(document.getElementById('browser-reopen').hidden).toBe(false);
      expect(document.getElementById('browser-reopen').textContent).toBe('Open browser');
      expect(document.getElementById('browser-reopen').dataset.operation).toBe(operationId);
    });

  it('keeps the clicked provider busy in place until its sign-in is selected', () => {
    mount();
    const choosing = { ...initial(), next_task: 'account-connection', allowed_actions: ['account.connect'], authentication: signIn() };
    const busy = () => [...document.querySelectorAll('[data-provider]')].filter(button => button.getAttribute('aria-busy') === 'true')
      .map(button => button.dataset.provider);
    const setupCode = document.getElementById('setup-code-open');
    renderSetup(document, { snapshot: choosing, connected: true });
    expect(setupCode.hidden).toBe(false);
    expect(setupCode.disabled).toBe(false);
    renderSetup(document, { snapshot: choosing, connected: true, acting: 'account.connect', provider: 'google' });
    expect(busy()).toEqual(['google']);
    expect(setupCode.hidden).toBe(true);
    expect(setupCode.disabled).toBe(true);
    expect(document.getElementById('task-status').textContent).toBe('Finish signing in in your browser. This window continues by itself.');
    // While the command is pending and nothing is selected yet, Brain reports no sign-in projection.
    const accepted = { ...choosing, allowed_actions: [], pending_operations: [{ ...record, action: 'account.connect' }], authentication: null };
    renderSetup(document, { snapshot: accepted, connected: true, pending: operationId, provider: 'google' });
    expect(document.getElementById('sign-in-options').hidden).toBe(false);
    expect(busy()).toEqual(['google']);
    expect([...document.querySelectorAll('[data-provider]:not([hidden])')].every(button => button.disabled)).toBe(true);
    expect(document.getElementById('task-status').textContent).toBe('Finish signing in in your browser. This window continues by itself.');
    for (const id of ['task-action', 'browser-reopen']) expect(document.getElementById(id).hidden).toBe(true);
    const selected = { ...accepted, authentication: { ...signIn(), operation_id: operationId, browser_operation_id: operationId,
      state: 'selected', actions: { select: false, cancel: true, open_browser: true, switch_account: false } } };
    renderSetup(document, { snapshot: selected, connected: true, pending: operationId, provider: 'google' });
    expect(document.getElementById('sign-in-options').hidden).toBe(true);
    expect(busy()).toEqual([]);
    expect(document.getElementById('task-status').textContent).toBe('Finish signing in in your browser. This window continues by itself.');
    renderSetup(document, { snapshot: accepted, connected: true, pending: operationId, provider: 'google', error: 'browser_unavailable' });
    expect(busy()).toEqual([]);
    renderSetup(document, { snapshot: choosing, connected: true });
    expect(setupCode.hidden).toBe(false);
    expect(setupCode.disabled).toBe(false);
  });

  it('requires the current authentication projection before offering account sign-in or browser retry', () => {
    mount();
    renderSetup(document, { snapshot: { ...initial(), next_task: 'account-connection', allowed_actions: ['account.connect'],
      pending_operations: [{ ...record, action: 'account.connect' }] }, connected: true });
    for (const id of ['sign-in-options', 'task-action', 'browser-reopen']) expect(document.getElementById(id).hidden).toBe(true);
  });

  it.each([['browser-access', 'browser.access'], ['finish-setup', 'browser.pair']])(
    'keeps accepted %s browser recovery available before account sign-in', (next_task, action) => {
      mount();
      renderSetup(document, { snapshot: { ...initial(), next_task, allowed_actions: [],
        authentication: signIn(), pending_operations: [{ ...record, action }] },
      connected: true, pending: operationId });
      expect(document.getElementById('browser-reopen').hidden).toBe(false);
      expect(document.getElementById('browser-reopen').disabled).toBe(false);
      expect(document.getElementById('browser-reopen').textContent).toBe('Open browser');
      expect(document.getElementById('browser-reopen').dataset.operation).toBe(operationId);
      expect(document.getElementById('sign-in-options').hidden).toBe(true);
      expect(document.getElementById('task-action').hidden).toBe(true);
    });

  it.each([['browser-access', 'browser.access'], ['finish-setup', 'browser.pair']])(
    'targets only the visible %s operation and waits while automatic opening is pending', (next_task, action) => {
      mount();
      const other = { ...record, operation_id: '33333333-3333-4333-8333-333333333333', action: 'activation.review' };
      const operation = { ...record, action };
      const value = { ...initial(), next_task, allowed_actions: [], pending_operations: [other, operation] };
      const button = document.getElementById('browser-reopen');
      renderSetup(document, { snapshot: value, connected: true, pending: other.operation_id });
      expect(button.hidden).toBe(false);
      expect(button.dataset.operation).toBe(operationId);
      renderSetup(document, { snapshot: value, connected: true, browserLaunchPending: true });
      expect(button.hidden).toBe(true);
      expect(button.dataset.operation).toBe('');
      for (const snapshot of [
        { ...value, pending_operations: [other, operation, { ...operation, operation_id: '44444444-4444-4444-8444-444444444444' }] },
        { ...value, pending_operations: [other, { ...operation, state: 'outcome_unknown' }] },
        { ...value, pending_operations: [other, { ...operation, reason: 'authority_unavailable' }] },
        { ...value, pending_operations: [other] },
      ]) {
        renderSetup(document, { snapshot, connected: true });
        expect(button.hidden).toBe(true);
        expect(button.dataset.operation).toBe('');
      }
    });
  it('does not offer browser recovery while the desktop status is uncertain', () => {
    mount();
    const value = { ...initial(), pending_operations: [record], allowed_actions: [] };
    for (const error of ['status_unavailable', 'operation_unconfirmed', 'browser_preparing', 'authority_unavailable']) {
      renderSetup(document, { snapshot: value, connected: true, error });
      expect(document.getElementById('browser-reopen').hidden).toBe(true);
    }
  });
  it('shows loading while waiting for the authoritative first snapshot', () => {
    mount();
    renderSetup(document, { snapshot: null, connected: false });
    expect(document.querySelector('main').dataset.journeyState).toBe('desktop.setup_loading');
    expect(document.getElementById('task-action').hidden).toBe(true);
  });

  it('shows account and computer inline with a separate explicit connection action', () => {
    mount();
    const value = { ...initial(), next_task: 'account-connection', allowed_actions: ['account.confirm'],
      pending_operations: [{ ...record, action: 'account.connect' }],
      decision: { operation_id: operationId, action: 'connect-account', creator_id: 'creator-selected',
        computer_name: 'Studio computer', expires_at: '2030-01-01T00:00:00Z' } };
    renderSetup(document, { snapshot: value, connected: true });
    expect(document.getElementById('decision-details').hidden).toBe(false);
    expect(document.getElementById('decision-account').textContent).toContain('creator-selected');
    expect(document.getElementById('decision-computer').textContent).toBe('Studio computer');
    expect(document.getElementById('task-action').dataset.action).toBe('account.confirm');
    expect(document.getElementById('task-action').disabled).toBe(false);
    expect(document.getElementById('browser-reopen').hidden).toBe(true);
    renderSetup(document, { snapshot: value, connected: false, error: 'status_unavailable' });
    expect(document.getElementById('task-action').disabled).toBe(true);
  });

  it.each([
    [{ display_name: 'Mia Studio', username: 'mia.studio' }, 'Mia Studio', '@mia.studio', 'M'],
    [{ display_name: 'Mia Studio', username: null }, 'Mia Studio', 'creator account creator-selected', 'M'],
    [{ display_name: null, username: 'mia.studio' }, '@mia.studio', '', 'M'],
    [null, 'creator account creator-selected', '', ''],
  ])('shows the account number only when no handle identifies the creator account', (label, name, identifier, letter) => {
    mount();
    renderSetup(document, { snapshot: { ...initial(), next_task: 'account-connection', allowed_actions: ['account.confirm'],
      pending_operations: [{ ...record, action: 'account.connect' }], creator_label: label && { creator_id: 'creator-selected', ...label },
      decision: { operation_id: operationId, action: 'connect-account', creator_id: 'creator-selected',
        computer_name: 'Studio computer', expires_at: '2030-01-01T00:00:00Z' } }, connected: true });
    expect(document.getElementById('decision-account').textContent).toBe(name);
    expect(document.getElementById('decision-account-identifier').textContent).toBe(identifier);
    expect(document.getElementById('decision-account-identifier').hidden).toBe(!identifier);
    expect(document.getElementById('decision-account-avatar').textContent).toBe(letter);
    expect(document.getElementById('decision-account-glyph').hidden).toBe(Boolean(letter));
    if (label?.username) expect(document.getElementById('decision-details').textContent).not.toContain('creator-selected');
  });

  it.each([
    [{ creator_id: 'creator-selected', display_name: 'Mia Studio', username: 'mia.studio' }, null, 'Mia Studio', '@mia.studio', 'M'],
    [{ creator_id: 'creator-selected', display_name: 'Mia Studio', username: null }, null, 'Mia Studio', '', 'M'],
    [{ creator_id: 'creator-selected', display_name: null, username: 'mia.studio' }, null, '@mia.studio', '', 'M'],
    [{ creator_id: 'creator-selected', display_name: 'Mia Studio', username: 'mia.studio' }, 'creator-other', null],
    [null, null, null],
  ])('names the labelled creator account in the header', (label, decisionCreator, name, handle, letter) => {
    mount();
    renderSetup(document, { snapshot: { ...initial(), next_task: 'finish-setup', allowed_actions: ['browser.pair'], creator_label: label,
      decision: decisionCreator && { operation_id: operationId, action: 'connect-account', creator_id: decisionCreator,
        computer_name: 'Studio computer', expires_at: '2030-01-01T00:00:00Z' } }, connected: true });
    expect(document.getElementById('header-account').hidden).toBe(!name);
    if (!name) return;
    expect(document.getElementById('header-account-name').textContent).toBe(name);
    expect(document.getElementById('header-account-handle').textContent).toBe(handle);
    expect(document.getElementById('header-account-handle').hidden).toBe(!handle);
    expect(document.getElementById('header-account-avatar').textContent).toBe(letter);
  });

  it('distinguishes browser sign-in from accepted work without declaring local readiness', () => {
    mount();
    for (const [state, slot, message] of [['awaiting-authentication', 'task-status', 'Finish signing in in your browser. This window continues by itself.'],
      ['accepted', 'task-pending', 'Account connected. Moving to the next step…']]) {
      renderSetup(document, { snapshot: { ...initial(), next_task: 'account-connection', allowed_actions: [],
        decision_status: { operation_id: operationId, state }, pending_operations: [{ ...record, action: 'account.connect' }] }, connected: true });
      expect(document.getElementById(slot).textContent).toBe(message);
      expect(document.getElementById('task-action').hidden).toBe(true);
      expect(document.getElementById('open-dashboard')).toBeNull();
    }
  });

  it('shows automatic local configuration without a Continue action', () => {
    mount();
    const value = { ...initial(), next_task: 'finish-setup', allowed_actions: ['setup.finalize'],
      facts: { ...initial().facts, installation: 'verified', account: 'verified' } };
    for (const snapshot of [value, { ...value, allowed_actions: [],
      pending_operations: [{ ...record, action: 'setup.finalize' }] }]) {
      renderSetup(document, { snapshot, connected: true });
      expect(document.getElementById('task-action').hidden).toBe(true);
      expect(document.getElementById('task-pending').textContent).toBe('Preparing this computer…');
    }
    renderSetup(document, { snapshot: { ...value, allowed_actions: [], reason: 'authority_unavailable' }, connected: true });
    expect(document.getElementById('task-action').hidden).toBe(true);
    expect(document.getElementById('task-status').textContent).toBe('Account authorization is unavailable.');
    renderSetup(document, { snapshot: { ...value, allowed_actions: [], reason: 'authority_unavailable' }, connected: true,
      savedAuthorizationExpired: true });
    expect(document.getElementById('task-status').textContent).toBe('Account authorization has expired. New capture and analysis are paused on this computer.');
  });

  it('renders the three local setup tasks and optional activation from one projection', () => {
    mount();
    for (const [next_task, anchor] of [['browser-access', 'browser_access'], ['account-connection', 'account_connection'], ['finish-setup', 'finish']]) {
      renderSetup(document, { snapshot: { ...initial(), next_task }, connected: true, deliberate: false });
      expect(document.querySelector('main').dataset.journeyState).toBe('desktop.setup_' + anchor);
      expect(document.querySelector('[aria-current="step"]').dataset.step).toBe(next_task);
    }
    renderSetup(document, { snapshot: { ...initial(), next_task: 'complete', local_ready: true, full_ready: false, allowed_actions: ['activation.review'] }, connected: true });
    expect(document.querySelector('main').dataset.journeyState).toBe('desktop.setup_complete');
    expect(document.getElementById('open-dashboard')).toBeNull();
    expect(document.getElementById('task-action').hidden).toBe(true);
  });

  it('moves focus and scroll only after a deliberate transition', () => {
    mount();
    const heading = document.getElementById('task-title');
    const focus = vi.spyOn(heading, 'focus');
    heading.scrollIntoView = vi.fn();
    renderSetup(document, { snapshot: initial(), connected: true, deliberate: false });
    expect(focus).not.toHaveBeenCalled();
    renderSetup(document, { snapshot: initial(), connected: true, deliberate: true });
    expect(focus).toHaveBeenCalledOnce();
    expect(heading.scrollIntoView).toHaveBeenCalledOnce();
  });

  it('does not ask the user to check status during automatic account recovery', () => {
    mount();
    renderSetup(document, { snapshot: { ...initial(), next_task: 'account-connection',
      pending_operations: [{ ...record, action: 'account.connect', state: 'outcome_unknown' }],
      allowed_actions: [] }, connected: true, error: 'operation_unconfirmed' });
    expect(document.getElementById('task-reconcile')).toBeNull();
    expect(document.getElementById('task-status').textContent).not.toMatch(/check|copy|paste|package/iu);
    expect(document.querySelector('textarea')).toBeNull();
  });

  it.each(['browser-access', 'account-connection'])('does not flash recovery actions while %s is being accepted', task => {
    mount();
    renderSetup(document, { snapshot: { ...initial(), next_task: task }, pending: operationId, connected: true });
    expect(document.getElementById('task-reconcile')).toBeNull();
    expect(document.getElementById('browser-reopen').hidden).toBe(true);
    expect(document.getElementById('setup-code-open').hidden).toBe(true);
  });

  it('shows the accepted operation failure instead of an indefinite waiting message', () => {
    mount();
    renderSetup(document, { snapshot: { ...initial(), next_task: 'account-connection',
      pending_operations: [{ ...record, action: 'account.connect', state: 'outcome_unknown', reason: 'authority_unavailable' }],
      allowed_actions: [] }, connected: true });
    expect(document.getElementById('task-status').textContent).toBe('Account authorization is unavailable.');
    expect(document.getElementById('browser-reopen').hidden).toBe(true);
    expect(document.getElementById('setup-code-open').hidden).toBe(true);
    expect([...document.querySelectorAll('.setup-actions')].every(group => group.hidden)).toBe(true);
  });

  it('shows no task action for an admitted sign-in with no decision', () => {
    mount();
    const admitted = {
      operation_id: operationId,
      browser_operation_id: '44444444-4444-4444-8444-444444444444',
      state: 'admitted',
      choices: { google: true, 'microsoft-personal': false, telegram: false, tiktok: false, default: true },
      actions: { select: false, cancel: false, open_browser: false, switch_account: false },
      authentication_attempt: { authentication_attempt_id: '55555555-5555-4555-8555-555555555555', provider: 'google', mode: 'sign-in' },
    };
    for (const snapshot of [
      { ...initial(), next_task: 'account-connection', allowed_actions: [], decision: null,
        pending_operations: [{ ...record, action: 'account.connect', state: 'waiting' }], authentication: admitted },
      { ...initial(), next_task: 'account-connection', allowed_actions: ['account.connect'], decision: null,
        continuation_available: false, authentication: admitted },
    ]) {
      renderSetup(document, { snapshot, connected: true });
      const action = document.getElementById('task-action');
      expect(action.hidden).toBe(true);
      expect(action.dataset.action).toBe('');
      expect(action.textContent).toBe('');
      expect(action.textContent).not.toBe('Continue');
    }
  });

  it('shows factual unavailable status and disables stale actions', () => {
    mount();
    renderSetup(document, { snapshot: initial(), connected: false, error: 'status_unavailable' });
    expect(document.querySelector('main').dataset.journeyState).toBe('desktop.setup_unavailable');
    expect(document.getElementById('task-action').disabled).toBe(true);
    expect(document.getElementById('task-reconnect')).toBeNull();
    expect(document.getElementById('task-status').textContent).not.toMatch(/saved data|removed|safe/iu);
    renderSetup(document, { snapshot: null, connected: false, error: 'status_unavailable' });
    expect(document.getElementById('task-description').hidden).toBe(true);
    expect(document.getElementById('task-title').textContent).not.toMatch(/loading/iu);
  });

  it('uses the saved app theme while leaving system mode to the shared tokens', () => {
    mount();
    applyTheme(document, { getItem: () => 'dark' });
    expect(document.documentElement.dataset.colorScheme).toBe('dark');
    document.documentElement.removeAttribute('data-color-scheme');
    applyTheme(document, { getItem: () => 'system' });
    expect(document.documentElement.dataset.colorScheme).toBeUndefined();
  });

  it('does not ask for a status check or dashboard action after local setup completes', () => {
    mount();
    renderSetup(document, { snapshot: { ...initial(), next_task: 'complete', local_ready: true, full_ready: false,
      decision_status: { operation_id: operationId, state: 'accepted' },
      pending_operations: [{ ...record, action: 'activation.review', state: 'outcome_unknown' }] }, connected: true, continuing: true });
    expect(document.getElementById('open-dashboard')).toBeNull();
    expect(document.getElementById('task-reconcile')).toBeNull();
    expect(document.getElementById('task-status').textContent).toBe('Opening dashboard…');
  });
  it('shows automatic passkey status recovery without asking the user to retry', () => {
    mount();
    const snapshot = { ...initial(), next_task: 'finish-setup', allowed_actions: ['browser.pair'] };
    const authentication = { enrolled: null, checked: true, authenticated: false, busy: false,
      error: 'passkey_status_network', recoveringStatus: true };
    const get = id => document.getElementById(id);
    renderSetup(document, { snapshot, connected: true, authentication });
    expect(get('task-title').textContent).toBe('Checking this computer…');
    expect(get('local-auth-status').textContent).toBe('');
    expect(document.querySelector('.setup-task-card').getAttribute('aria-busy')).toBe('true');
    expect(get('local-authenticate').hidden).toBe(true);
    expect(get('local-auth-retry')).toBeNull();
    expect(get('task-action').disabled).toBe(true);
    renderSetup(document, { snapshot, connected: true, authentication: { ...authentication, recoveringStatus: false } });
    expect(get('task-description').hidden).toBe(true);
    expect(get('local-auth-status').textContent).toBe('This page updates by itself.');
    expect(get('local-auth-status').querySelector('[data-status-icon="attention"]')).toBeTruthy();
    expect(get('local-authenticate').hidden).toBe(true);
    renderSetup(document, { snapshot, connected: true, authentication: { ...authentication, enrolled: true, recoveringStatus: false, error: null } });
    expect(get('local-authenticate').hidden).toBe(false);
    expect(get('local-authenticate').textContent).toBe('Sign in with passkey');
  });
  it('requires explicit local authentication before pairing or opening the dashboard', () => {
    mount();
    const snapshot = { ...initial(), next_task: 'finish-setup', allowed_actions: ['browser.pair'] };
    for (const enrolled of [false, true]) {
      renderSetup(document, { snapshot, connected: true,
        authentication: { enrolled, checked: true, authenticated: false, busy: false } });
      expect(document.getElementById('local-access').hidden).toBe(false);
      expect(document.getElementById('local-access').dataset.journeyState).toBe('desktop.setup_local_access');
      expect(document.getElementById('local-authenticate').textContent).toBe(enrolled ? 'Sign in with passkey' : 'Create passkey');
      expect(document.getElementById('task-action').hidden).toBe(true);
      expect(document.getElementById('task-action').dataset.action).toBe('');
      expect(document.getElementById('task-title').textContent).toBe(enrolled ? 'Confirm it’s you' : 'Protect your analytics on this computer');
      expect(document.getElementById('task-action').disabled).toBe(true);
      expect(document.getElementById('open-dashboard')).toBeNull();
    }
    renderSetup(document, { snapshot, connected: true,
      authentication: { enrolled: true, checked: true, authenticated: true, busy: false } });
    expect(document.getElementById('local-access').hidden).toBe(true);
    expect(document.getElementById('task-action').disabled).toBe(false);
    expect(document.getElementById('task-action').hidden).toBe(false);
    expect(document.getElementById('task-action').textContent).toBe('Connect extension');
    expect(document.getElementById('task-title').textContent).toBe('Connect the extension to this app');
    expect(document.getElementById('task-description').textContent).toBe('New messages show up in your analytics once it’s connected.');
  });
  it('shows pairing as a single explicit action only after fresh local access and suppresses it during connection', () => {
    mount();
    const snapshot = { ...initial(), next_task: 'finish-setup', allowed_actions: ['browser.pair'] };
    const authentication = { enrolled: true, checked: true, authenticated: true, busy: false };
    const get = id => document.getElementById(id);
    for (const state of [
      { connected: false, authentication },
      { connected: true, authentication: { ...authentication, checked: false } },
      { connected: true, authentication: { ...authentication, busy: true } },
      { connected: true, authentication: { ...authentication, error: 'passkey_unconfirmed' } },
      { connected: true, authentication, error: 'status_unavailable' },
    ]) {
      renderSetup(document, { snapshot, ...state });
      expect(get('task-action').hidden).toBe(true);
      expect(get('task-action').dataset.action).toBe('');
    }
    renderSetup(document, { snapshot, connected: true, authentication, acting: 'browser.pair' });
    expect(get('task-action').hidden).toBe(false);
    expect(get('task-action').disabled).toBe(true);
    expect(get('task-title').textContent).toBe('Connect the extension to this app');
    renderSetup(document, { snapshot: { ...snapshot, allowed_actions: [], pending_operations: [
      { ...record, action: 'browser.pair', state: 'waiting' },
    ] }, connected: true, authentication, browserLaunchPending: true });
    expect(get('task-action').hidden).toBe(true);
    expect(get('browser-reopen').hidden).toBe(true);
    expect(get('task-title').textContent).toBe('Connecting the extension');
    expect(get('task-description').textContent).toBe('New messages show up in your analytics once it’s connected.');
    expect(get('task-pending').textContent).toBe('Keep the browser open. This window continues by itself.');
    expect(get('task-status').textContent).toBe('');
  });
  it.each([
    ['renewing', 'Renewing access on this computer', 'This page updates when renewal finishes.', 'Renewal needs an internet connection.'],
    ['expired', 'Account authorization has expired', 'New capture and analysis are paused on this computer.', ''],
    ['removed', 'Access removed from this computer', 'This computer can no longer open analytics.', ''],
  ])('names %s access on this computer instead of offering another attempt', (access, heading, description, guidance) => {
    mount();
    const snapshot = { ...initial(), next_task: 'finish-setup', allowed_actions: ['browser.pair'],
      pending_operations: [{ ...record, action: 'browser.pair', state: 'outcome_unknown', reason: 'authority_unavailable' }] };
    const authentication = { checked: true, enrolled: null, authenticated: false, busy: false, error: null, access };
    renderSetup(document, { snapshot, connected: true, authentication });
    const get = id => document.getElementById(id);
    expect(get('local-access').hidden).toBe(!guidance);
    expect(get('task-title').textContent).toBe(heading);
    expect(get('task-description').textContent).toBe(description);
    expect(get('local-auth-status').textContent).toBe(guidance);
    if (access === 'renewing') expect(get('local-auth-status').querySelector('[data-status-icon="info"]')).toBeTruthy();
    expect(get('local-authenticate').hidden).toBe(true);
    expect(get('local-auth-retry')).toBeNull();
    expect(get('task-status').textContent).toBe('');
    expect(get('task-action').disabled).toBe(true);
    expect(get('browser-reopen').hidden).toBe(true);
    const shown = [...document.querySelectorAll('button')].filter(button => !button.closest('[hidden]'));
    expect(shown.map(button => button.textContent)).not.toContain('Try again');
    renderSetup(document, { snapshot, connected: false, error: 'status_unavailable', authentication });
    expect(get('task-status').textContent).toBe('The setup connection was interrupted. Reconnecting…');
    renderSetup(document, { snapshot, connected: true, authentication: { ...authentication, enrolled: true, access: null } });
    expect(get('task-description').textContent).toBe('Your computer will ask you to confirm it’s you.');
    expect(get('local-auth-status').textContent).toBe('');
    expect(get('local-authenticate').hidden).toBe(false);
    expect(get('task-status').textContent).toBe('Account authorization is unavailable.');
  });
});

it('reads pushed frames across chunk boundaries and reports disconnection', async () => {
  const text = 'event: setup\ndata: ' + JSON.stringify(initial()) + '\n\n';
  const data = new TextEncoder().encode(text);
  const stream = new ReadableStream({ start(controller) { controller.enqueue(data.slice(0, 14)); controller.enqueue(data.slice(14)); controller.close(); } });
  const receive = vi.fn();
  await expect(setupEvents(async () => new Response(stream, { headers: { 'content-type': 'text/event-stream' } }), {}, new AbortController().signal, receive)).rejects.toThrow('stream_unavailable');
  expect(receive).toHaveBeenCalledWith(JSON.stringify(initial()));
});

it('recognizes the planned setup stream end', async () => {
  const stream = new ReadableStream({ start(controller) {
    controller.enqueue(new TextEncoder().encode('event: setup-end\ndata: {}\n\n'));
    controller.close();
  } });
  await expect(setupEvents(async () => new Response(stream, { headers: { 'content-type': 'text/event-stream' } }),
    {}, new AbortController().signal, vi.fn())).rejects.toThrow('stream_expired');
});

it('reconnects after a planned setup stream end without an interruption message', async () => {
  vi.useFakeTimers();
  const received = [], opened = vi.fn();
  const subject = new DesktopSetupClient({ generation, storage: storage(), receive: value => received.push(value),
    request: async () => response(session),
    stream: async (_request, _headers, _signal, publish) => {
      opened();
      publish(JSON.stringify(initial()));
      throw Error('stream_expired');
    } });
  try {
    await subject.connect();
    expect(received.every(value => value.error === null)).toBe(true);
    expect(opened).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(opened).toHaveBeenCalledTimes(2);
    expect(received.every(value => value.error === null)).toBe(true);
  } finally { subject.stop(); vi.useRealTimers(); }
});

it('rejects dashboard and Chrome modules anywhere in the built setup graph', () => {
  const bundle = { 'setup.js': { type: 'chunk', isEntry: true, facadeModuleId: '/frontend/onboarding/index.html', fileName: 'setup.js', modules: {}, imports: ['shared.js'], dynamicImports: [] },
    'shared.js': { type: 'chunk', modules: { '/frontend/shared-setup/presentation.js': {} }, imports: [], dynamicImports: [] } };
  expect(() => assertSetupIsolation(bundle)).not.toThrow();
  for (const forbidden of ['/frontend/src/App.tsx', '/frontend/src/store/analyticsStore.ts', '/frontend/src/services/websocketService.ts', '/frontend/browser-tasks/chrome.js', '/node_modules/ajv/lib/ajv.js']) {
    bundle['shared.js'].modules = { [forbidden]: {} };
    expect(() => assertSetupIsolation(bundle)).toThrow('runtime or browser dependency');
  }
});

it.each([
  [assertSetupIsolation, '/frontend/onboarding/index.html'],
  [assertDashboardIsolation, '/frontend/index.html'],
  [assertReceivingIsolation, '/frontend/setup-code/index.html'],
])('rejects a Chrome API in every native or receiving entry dependency', (check, path) => {
  const bundle = { 'entry.js': { type: 'chunk', isEntry: true, facadeModuleId: path,
    fileName: 'entry.js', modules: {}, imports: ['child.js'], dynamicImports: [] },
    'child.js': { type: 'chunk', modules: {}, imports: [], dynamicImports: [], code: '' } };
  expect(() => check(bundle)).not.toThrow();
  for (const code of ['chrome.runtime.connect()', 'globalThis.chrome["runtime"].connect()', 'browser.runtime.sendMessage()', 'chrome?.runtime.connect()', 'browser?.["runtime"].sendMessage()']) {
    bundle['child.js'].code = code;
    expect(() => check(bundle)).toThrow('cannot reference Chrome APIs');
  }
});

describe('helper notices in the desktop', () => {
  const sessionValue = generation => ({ role: 'desktop', generation, stage: 'setup', csrf_token: 'A'.repeat(43) });
  it('shows a helper notice next to the step and clears it when Brain clears it', async () => {
    const generation = crypto.randomUUID(), received = [];
    let notices;
    const request = vi.fn(async () => ({ ok: true, status: 200, text: async () => JSON.stringify(sessionValue(generation)) }));
    const client = new DesktopSetupClient({ generation, storage: { getItem: () => null, setItem() {}, removeItem() {} },
      receive: state => received.push(state), request,
      stream: async (_request, _headers, _signal, publish, notice) => {
        notices = notice; publish(JSON.stringify({ ...initial(), generation }));
      } });
    await client.connect();
    notices(JSON.stringify({ notices: [{ task_id: crypto.randomUUID(), reason: 'profile_mismatch' }] }));
    expect(received.at(-1).error).toBe('profile_mismatch');
    notices(JSON.stringify({ notices: [] }));
    expect(received.at(-1).error).toBe(null);
    expect(() => notices(JSON.stringify({ notices: [{ task_id: 'x', reason: 'profile_mismatch' }] }))).toThrow('invalid_snapshot');
    for (const reason of ['needs_account', 'needs_consent', 'needs_permission']) {
      notices(JSON.stringify({ notices: [{ task_id: crypto.randomUUID(), reason }] }));
      expect(received.at(-1).error).toBe('browser_' + reason);
    }
    client.stop();
  });
  it('reads browser notice events and ignores unknown events', async () => {
    const encoder = new TextEncoder(), seen = [], shown = [];
    const body = new ReadableStream({ start(controller) {
      controller.enqueue(encoder.encode('event: other\ndata: {}\n\nevent: browser-notices\ndata: {"notices":[]}\n\n'));
      controller.close();
    } });
    const request = async () => ({ ok: true, body, headers: new Headers({ 'content-type': 'text/event-stream' }) });
    await expect(setupEvents(request, {}, new AbortController().signal, value => seen.push(value), value => shown.push(value)))
      .rejects.toThrow('stream_unavailable');
    expect(seen).toEqual([]);
    expect(shown).toEqual(['{"notices":[]}']);
  });
});

it('names only the current deliberate desktop action while the owner receipt is outstanding', async () => {
  const states = [];
  const subject = new DesktopSetupClient({ generation, storage: storage(), receive: value => states.push(value),
    uuid: () => operationId, stream: () => () => {}, request: async url => {
      if (url.endsWith('/timing')) return response({});
      throw new TypeError('lost response');
    } });
  subject.value = initial(); subject.session = session; subject.connected = true;
  await subject.act('browser.access');
  expect(states.at(-1)).toMatchObject({ pending: operationId, pendingAction: 'browser.access', error: 'operation_unconfirmed' });
  subject.remember(null);
  subject.notify();
  expect(states.at(-1)).not.toHaveProperty('pendingAction');
  subject.stop();
});

it.each(['activation.review', 'activation.confirm'])('submits a fresh explicit replacement review without launching the old browser task (%s)', async localAction => {
  const calls = [];
  const openBrowser = vi.fn();
  const old = { ...record, action: 'activation.review', state: 'outcome_unknown', reason: 'operation_unconfirmed' };
  const successorId = '33333333-3333-4333-8333-333333333333';
  const subject = new DesktopSetupClient({ generation, storage: storage(), receive() {},
    uuid: () => successorId, stream: () => () => {}, openBrowser, request: async (url, options) => {
      calls.push([url, options?.body && JSON.parse(options.body)]);
      return response({ ...old, operation_id: successorId, state: 'accepted', reason: 'none' }, 202);
    } });
  const child = { ...old, action: 'activation.confirm', operation_id: '44444444-4444-4444-8444-444444444444' };
  subject.value = { ...initial(), pending_operations: localAction === 'activation.review' ? [old] : [old, child], allowed_actions: ['activation.review'] };
  subject.session = session; subject.connected = true; subject.remember(localAction === 'activation.review' ? old.operation_id : child.operation_id);
  await subject.act('activation.review');
  const commands = calls.filter(([url]) => url.endsWith('/commands'));
  expect(commands).toHaveLength(1);
  expect(commands[0][1]).toMatchObject({ action: 'activation.review', operation_id: successorId });
  expect(openBrowser).not.toHaveBeenCalled();
});

it('requires the explicit protected recovery discriminator and never opens an ordinary browser task', async () => {
  const calls = [];
  const openBrowser = vi.fn();
  const parent = { ...record, action: 'account.connect', state: 'waiting' };
  const subject = new DesktopSetupClient({ generation, storage: storage(), receive() {},
    uuid: () => operationId, stream: () => () => {}, openBrowser,
    request: async (url, options) => {
      calls.push([url, options?.body && JSON.parse(options.body)]);
      return response(parent, url.endsWith('/commands') ? 202 : 200);
    } });
  subject.value = { ...initial(), next_task: 'account-connection', authority_recovery: 'available',
    allowed_actions: ['account.connect'] };
  subject.session = session;
  subject.connected = true;
  await expect(subject.act('account.connect')).rejects.toThrow('action_unavailable');
  await expect(subject.act('account.connect', { provider: 'default', recover_authority: true })).rejects.toThrow('action_unavailable');
  await subject.act('account.connect', { recover_authority: true });
  const commands = calls.filter(([url]) => url.endsWith('/commands'));
  expect(commands).toHaveLength(1);
  expect(commands[0][1]).toMatchObject({ action: 'account.connect', recover_authority: true });
  expect(openBrowser).not.toHaveBeenCalled();
  subject.value = { ...subject.value, authority_recovery: 'sign-in-required', pending_operations: [parent], allowed_actions: [] };
  await expect(subject.launchBrowser(true, operationId)).rejects.toThrow('browser_unavailable');
  expect(openBrowser).not.toHaveBeenCalled();
});

it('admits protected child commands only with the explicit marker and never launches the ordinary browser', async () => {
  const calls = [];
  const parent = { ...record, action: 'account.connect', state: 'waiting' };
  const child = { ...record, operation_id: '33333333-3333-4333-8333-333333333333', action: 'auth.select', state: 'accepted' };
  const openBrowser = vi.fn();
  const subject = new DesktopSetupClient({ generation, storage: storage(), receive() {},
    uuid: () => child.operation_id, stream: () => () => {}, openBrowser,
    request: async (url, options) => {
      calls.push([url, options?.body && JSON.parse(options.body)]);
      return response(child, url.endsWith('/commands') ? 202 : 200);
    } });
  subject.value = { ...initial(), next_task: 'account-connection', authority_recovery: 'sign-in-required',
    pending_operations: [parent], allowed_actions: ['auth.select'],
    authentication: { operation_id: parent.operation_id, browser_operation_id: null, state: 'selection-required',
      choices: { default: true, google: false, 'microsoft-personal': false, telegram: false, tiktok: false },
      actions: { select: true, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null } };
  subject.session = session;
  subject.connected = true;
  subject.remember(parent.operation_id);
  await expect(subject.act('auth.select', { provider: 'default' })).rejects.toThrow('action_unavailable');
  await expect(subject.act('auth.select', { provider: 'google', recover_authority: true })).rejects.toThrow('action_unavailable');
  await subject.act('auth.select', { provider: 'default', recover_authority: true });
  const commands = calls.filter(([url]) => url.endsWith('/commands'));
  expect(commands).toHaveLength(1);
  expect(commands[0][1]).toMatchObject({ action: 'auth.select', provider: 'default', recover_authority: true });
  expect(openBrowser).not.toHaveBeenCalled();
  subject.value = { ...subject.value, pending_operations: [parent, child], authority_recovery: 'checking', allowed_actions: [] };
  await expect(subject.launchBrowser(true, child.operation_id)).rejects.toThrow('browser_unavailable');
  expect(openBrowser).not.toHaveBeenCalled();
});

it('allows an explicit protected successor or cancellation while its exact parent remains pending', async () => {
  for (const [state, action, options] of [
    ['retired', 'auth.select', { provider: 'default', recover_authority: true }],
    ['selected', 'auth.cancel', { recover_authority: true }],
  ]) {
    const calls = [];
    const parent = { ...record, action: 'account.connect', state: 'waiting' };
    const child = { ...record, operation_id: '33333333-3333-4333-8333-333333333333', action, state: 'accepted' };
    const openBrowser = vi.fn();
    const subject = new DesktopSetupClient({ generation, storage: storage(), receive() {},
      uuid: () => child.operation_id, stream: () => () => {}, openBrowser,
      request: async (url, options) => {
        calls.push([url, options?.body && JSON.parse(options.body)]);
        return response(child, url.endsWith('/commands') ? 202 : 200);
      } });
    subject.value = { ...initial(), next_task: 'account-connection', authority_recovery: 'sign-in-required',
      pending_operations: [parent], allowed_actions: [action],
      authentication: { operation_id: parent.operation_id, browser_operation_id: null, state,
        choices: { default: true, google: false, 'microsoft-personal': false, telegram: false, tiktok: false },
        actions: { select: state === 'retired', cancel: state === 'selected', open_browser: false, switch_account: false },
        authentication_attempt: { authentication_attempt_id: '0198a1b2-c3d4-7300-8000-000000000063', provider: 'default', mode: 'sign-in' } } };
    subject.session = session;
    subject.connected = true;
    subject.remember(parent.operation_id);
    await subject.act(action, options);
    expect(calls.filter(([url]) => url.endsWith('/commands'))[0][1]).toMatchObject({ action, recover_authority: true });
    expect(openBrowser).not.toHaveBeenCalled();
  }
});

it('consumes a protected browser entry directly without creating helper storage', async () => {
  const destination = 'https://provider.example/onboarding/browser-ceremonies/' + 'B'.repeat(43);
  const replace = vi.fn(), scrub = vi.fn(), local = { setItem: vi.fn() };
  const request = vi.fn(async () => response({ protected_browser_url: destination }));
  await runInNewContext(entryScript, { URL, URLSearchParams,
    location: { pathname: '/browser-tasks/entry', hash: '#code=' + 'A'.repeat(43), replace },
    history: { replaceState: scrub }, document: { getElementById: () => ({}) }, sessionStorage: local, fetch: request });
  expect(scrub).toHaveBeenCalledBefore(request);
  expect(replace).toHaveBeenCalledExactlyOnceWith(destination);
  expect(local.setItem).not.toHaveBeenCalled();
  expect(request).toHaveBeenCalledOnce();
});

it.each(['http://provider.example/onboarding/browser-ceremonies/' + 'B'.repeat(43),
  'https://provider.example/other', 'https://provider.example/onboarding/browser-ceremonies/' + 'B'.repeat(43) + '?other=x',
  'https://username@provider.example/onboarding/browser-ceremonies/' + 'B'.repeat(43)])(
  'does not navigate an invalid protected entry response %s', async destination => {
    const replace = vi.fn(), local = { setItem: vi.fn() };
    await runInNewContext(entryScript, { URL, URLSearchParams,
      location: { pathname: '/browser-tasks/entry', hash: '#code=' + 'A'.repeat(43), replace },
      history: { replaceState: vi.fn() }, document: { getElementById: () => ({}) }, sessionStorage: local,
      fetch: vi.fn(async () => response({ protected_browser_url: destination })) });
    expect(replace).not.toHaveBeenCalled();
    expect(local.setItem).not.toHaveBeenCalled();
  });

const protectedSelected = () => ({ ...initial(), journey_id: operationId, next_task: 'account-connection',
  authority_recovery: 'sign-in-required', authentication: { operation_id: operationId, browser_operation_id: null,
    state: 'selected', actions: { select: false, cancel: true, open_browser: true, switch_account: false },
    authentication_attempt: { authentication_attempt_id: '0198a1b2-c3d4-7300-8000-000000000063', provider: 'default', mode: 'sign-in' } } });

it('protected launch requests only a one-use code for the exact selected child', async () => {
  const invoke = vi.fn(), value = protectedSelected();
  const client = { value, connected: true, json: vi.fn(async () => ({ entry_code: 'A'.repeat(43) })) };
  expect(await openProtectedBrowser(client, invoke)).toBe(true);
  expect(client.json).toHaveBeenCalledExactlyOnceWith('/api/v1/desktop/setup/protected-browser/open', {
    parent_operation_id: operationId, attempt_id: value.authentication.authentication_attempt.authentication_attempt_id,
    expected: { generation, revision: value.revision, scope_digest: value.scope_digest },
  });
  expect(invoke).toHaveBeenCalledExactlyOnceWith('A'.repeat(43));
  client.value.authentication.actions.open_browser = false;
  await expect(openProtectedBrowser(client, invoke)).rejects.toThrow('browser_unavailable');
  expect(client.json).toHaveBeenCalledOnce();
});

it('a stale protected launch response cannot launch a different child or workspace', async () => {
  const invoke = vi.fn(), value = protectedSelected();
  const client = { value, connected: true, json: vi.fn(async () => {
    client.value = { ...value, authority_recovery: 'checking', authentication: null };
    return { entry_code: 'A'.repeat(43) };
  }) };
  expect(await openProtectedBrowser(client, invoke)).toBe(false);
  expect(invoke).not.toHaveBeenCalled();
  expect(client.json).toHaveBeenCalledOnce();
});

it('renders the protected recovery sequence as one task and never exposes ordinary setup controls', () => {
  document.documentElement.innerHTML = markup;
  const render = (value, extra = {}) => renderSetup(document, { snapshot: value, connected: true, pending: null,
    authentication: { checked: true, authenticated: false, access: 'expired', enrolled: null, busy: false }, ...extra });
  const primary = document.getElementById('task-action'), cancel = document.getElementById('protected-cancel');
  const base = { ...initial(), next_task: 'account-connection', authority_recovery: 'available', allowed_actions: ['account.connect'] };
  render(base);
  expect(document.getElementById('task-title').textContent).toBe('Sign in to open your analytics');
  expect(document.getElementById('task-description').textContent).toBe('Use the same sign-in as before.');
  expect(primary.textContent).toBe('Sign in');
  expect(primary.dataset.protectedRecovery).toBe('true');
  expect(primary.dataset.action).toBe('account.connect');
  expect(document.querySelector('main nav').hidden).toBe(true);
  for (const id of ['local-access', 'setup-code-open', 'sign-in-options', 'browser-reopen', 'decision-details']) expect(document.getElementById(id).hidden).toBe(true);
  render({ ...base, allowed_actions: [] });
  expect(primary.hidden).toBe(true);
  render({ ...base, authority_recovery: 'preparing', allowed_actions: [] });
  expect(primary.hidden).toBe(true);
  expect(document.getElementById('task-pending').textContent).toBe('Preparing sign-in\u2026');
  expect(document.getElementById('task-pending').querySelector('[data-status-icon="pending"] circle')).toBeTruthy();
  const selected = protectedSelected();
  const auth = { ...selected.authentication, choices: { default: true, google: false }, authentication_attempt: null,
    state: 'selection-required', actions: { select: true, cancel: false, open_browser: false, switch_account: false } };
  render({ ...selected, authentication: auth, allowed_actions: ['auth.select'] });
  expect(primary.textContent).toBe('Sign in');
  expect(primary.disabled).toBe(false);
  expect(authorityRecoveryOptions(primary.dataset.action)).toEqual({ recover_authority: true, provider: 'default' });
  // The explicit sign-in continues by itself, so neither later step offers a click.
  render({ ...selected, authentication: auth, allowed_actions: ['auth.select'] }, { recoverySignInPending: true });
  expect(primary.hidden).toBe(true);
  expect(document.getElementById('task-pending').textContent).toBe('Preparing sign-in\u2026');
  render({ ...selected, authority_recovery_browser: 'ready', allowed_actions: ['auth.cancel'] }, { recoverySignInPending: true });
  expect(primary.hidden).toBe(true);
  expect(document.getElementById('task-pending').textContent).toBe('Opening browser\u2026');
  expect(cancel.hidden).toBe(true);
  render({ ...selected, authority_recovery_browser: 'ready', allowed_actions: ['auth.cancel'] });
  expect(primary.textContent).toBe('Open browser');
  expect(primary.dataset.action).toBe('browser.open');
  expect(cancel.hidden).toBe(false);
  expect(cancel.classList.contains('secondary')).toBe(true);
  render({ ...selected, authority_recovery_browser: 'ready', allowed_actions: ['auth.cancel'] }, { error: 'chrome_unavailable' });
  expect(document.getElementById('task-description').textContent).toBe('Sign in using your browser.');
  expect(document.getElementById('task-status').textContent).toBe('Install the required browser to continue.');
  render({ ...selected, authority_recovery_browser: 'launch-claimed', allowed_actions: ['auth.cancel'] });
  expect(primary.hidden).toBe(true);
  expect(document.getElementById('task-pending').textContent).toBe('Waiting for browser\u2026');
  expect(document.getElementById('task-status').textContent).toBe('');
  render({ ...selected, authority_recovery_browser: 'handed-off', allowed_actions: ['auth.cancel'] });
  expect(document.getElementById('task-description').textContent).toBe('Finish signing in in your browser. This page continues by itself.');
  expect(document.getElementById('task-pending').textContent).toBe('Waiting for sign-in\u2026');
  render({ ...selected, authority_recovery_browser: 'handed-off', allowed_actions: ['auth.cancel'] }, { acting: 'auth.cancel' });
  expect(document.getElementById('task-pending').textContent).toBe('Canceling sign-in…');
  expect(cancel.hidden).toBe(true);
  render({ ...selected, authority_recovery_browser: 'unconfirmed', allowed_actions: ['auth.cancel'] });
  expect(document.getElementById('task-status').textContent).toBe('Sign-in isn’t confirmed.');
  expect(primary.hidden).toBe(true);
  expect(cancel.hidden).toBe(false);
  expect(cancel.classList.contains('secondary')).toBe(false);
  render({ ...selected, authentication: { ...auth, state: 'retired' }, allowed_actions: ['auth.select'] });
  expect(primary.textContent).toBe('Sign in');
  expect(document.getElementById('task-status').textContent).toBe('Sign-in didn’t finish.');
  expect(document.getElementById('task-status').querySelector('[data-status-icon="info"]')).toBeTruthy();
  expect(cancel.hidden).toBe(true);
  render({ ...base, authority_recovery: 'restoring', allowed_actions: [] });
  expect(document.getElementById('task-pending').textContent).toBe('Signed in. Getting this computer ready…');
  expect(primary.hidden).toBe(true);
  expect(cancel.hidden).toBe(true);
  render({ ...base, authority_recovery: 'blocked', allowed_actions: [] });
  expect(document.getElementById('task-title').textContent).toBe('Can’t open your analytics right now');
  expect(primary.hidden).toBe(true);
  expect(document.getElementById('task-description').textContent).toBe('This page updates by itself if anything changes.');
  render(initial(), { authentication: null });
  expect(document.querySelector('main nav').hidden).toBe(false);
  expect(primary.dataset.protectedRecovery).toBe('');
  expect(cancel.hidden).toBe(true);
});

it('protected recovery never enables stale controls during a command or disconnected presentation', () => {
  const value = { ...protectedSelected(), authority_recovery_browser: 'ready', allowed_actions: ['auth.cancel'] };
  expect(authorityRecoveryView({ snapshot: value, connected: false }).action).toBeNull();
  expect(authorityRecoveryView({ snapshot: value, connected: false, error: 'session_unavailable' }))
    .toMatchObject({ status: "Setup couldn't continue.", tone: 'error', action: null, cancel: false });
  expect(authorityRecoveryView({ snapshot: { ...value, journey_id: operationId }, connected: false, error: 'connection_unavailable' }))
    .toMatchObject({ description: '', status: 'Dipsy can’t connect right now. Your setup progress is saved.', tone: 'warning', action: null, cancel: false });
  expect(authorityRecoveryView({ snapshot: { ...value, journey_id: null }, connected: false, error: 'connection_unavailable' }))
    .toMatchObject({ description: '', status: 'Dipsy can’t connect right now.', tone: 'warning', action: null, cancel: false });
  expect(authorityRecoveryView({ snapshot: value, connected: true }, 'auth.cancel').action).toBeNull();
  expect(authorityRecoveryView({ snapshot: value, connected: true, pending: 'new', pendingAction: 'auth.cancel' }).cancel).toBe(false);
  expect(authorityRecoveryView({ snapshot: value, connected: true, browserLaunchPending: true }).action).toBeNull();
  expect(authorityRecoveryView({ snapshot: value, connected: true, error: 'chrome_unavailable' }).status).toBe('Install the required browser to continue.');
});

it('preflights Chrome before spending a protected launch and never auto-opens a replacement', async () => {
  const request = vi.fn(async () => response({ entry_code: 'A'.repeat(43) }));
  const invoke = vi.fn(async () => false);
  vi.stubGlobal('__TAURI__', { core: { invoke } });
  try {
    const subject = new DesktopSetupClient({ generation, storage: storage(), receive() {}, request, stream: () => () => {} });
    subject.value = { ...protectedSelected(), authority_recovery_browser: 'ready' };
    subject.connected = true; subject.session = session;
    await expect(subject.launchProtectedBrowser()).rejects.toThrow('chrome_unavailable');
    expect(invoke).toHaveBeenCalledExactlyOnceWith('desktop_chrome_available');
    expect(request).not.toHaveBeenCalled();
    subject.stop();
  } finally { vi.unstubAllGlobals(); }
});

it('opens the protected native entry once from the current explicit gesture', async () => {
  const invoke = vi.fn(async name => name === 'desktop_chrome_available' ? true : undefined);
  vi.stubGlobal('__TAURI__', { core: { invoke } });
  try {
    let subject;
    const request = vi.fn(async (url, options) => {
      expect(url).toBe('/api/v1/desktop/setup/protected-browser/open');
      expect(JSON.parse(options.body).attempt_id).toBe(subject.value.authentication.authentication_attempt.authentication_attempt_id);
      subject.value = { ...subject.value, authority_recovery_browser: 'launch-claimed',
        authentication: { ...subject.value.authentication, actions: { ...subject.value.authentication.actions, open_browser: false } } };
      return response({ entry_code: 'A'.repeat(43) });
    });
    subject = new DesktopSetupClient({ generation, storage: storage(), receive() {}, request, stream: () => () => {} });
    subject.value = { ...protectedSelected(), authority_recovery_browser: 'ready' };
    subject.connected = true; subject.session = session;
    await subject.launchProtectedBrowser();
    expect(invoke.mock.calls).toEqual([['desktop_chrome_available'], ['desktop_browser_task', { entryCode: 'A'.repeat(43) }]]);
    expect(request).toHaveBeenCalledOnce();
    await expect(subject.launchProtectedBrowser()).rejects.toThrow('browser_unavailable');
    expect(request).toHaveBeenCalledOnce();
    subject.stop();
  } finally { vi.unstubAllGlobals(); }
});

describe('one explicit protected sign-in', () => {
  const child = '33333333-3333-4333-8333-333333333333';
  const attempt = { authentication_attempt_id: '0198a1b2-c3d4-7300-8000-000000000063', provider: 'default', mode: 'sign-in' };
  const auth = (state, actions, authenticationAttempt = null) => ({ operation_id: operationId, browser_operation_id: null, state,
    choices: { default: true, google: false, 'microsoft-personal': false, telegram: false, tiktok: false },
    actions: { select: false, cancel: false, open_browser: false, switch_account: false, ...actions },
    authentication_attempt: authenticationAttempt });
  const parent = { ...record, action: 'account.connect', state: 'waiting' };
  const base = { ...initial(), next_task: 'account-connection', authority_recovery: 'available', allowed_actions: ['account.connect'] };
  const selection = revision => ({ ...base, revision, authority_recovery: 'sign-in-required', allowed_actions: ['auth.select'],
    pending_operations: [parent], authentication: auth('selection-required', { select: true }) });
  const ready = revision => ({ ...base, revision, authority_recovery: 'sign-in-required', authority_recovery_browser: 'ready',
    allowed_actions: ['auth.cancel'], pending_operations: [parent], authentication: auth('selected', { cancel: true, open_browser: true }, attempt) });
  const start = (clock = () => 1000) => {
    let publish, latest = base;
    const ids = [operationId, child], commands = [], opens = [], states = [];
    const subject = new DesktopSetupClient({ generation, storage: storage(), receive: state => states.push(state), uuid: () => ids.shift(), now: clock,
      stream: async (_request, _headers, _signal, listener) => {
        publish = next => { latest = next; listener(JSON.stringify(next)); };
        listener(JSON.stringify(latest));
      },
      request: async (url, options) => {
        const body = options?.body && JSON.parse(options.body);
        if (url.endsWith('/session')) return response(session);
        if (url.endsWith('/commands')) {
          commands.push(body);
          return response({ ...record, operation_id: body.operation_id, action: body.action, state: 'accepted' }, 202);
        }
        if (url.endsWith('/protected-browser/open')) { opens.push(body); return response({ entry_code: 'A'.repeat(43) }); }
        const id = url.split('/').pop();
        return response(id === child ? { ...record, operation_id: id, action: 'auth.select', state: 'completed' } : { ...parent, operation_id: id });
      } });
    return { subject, commands, opens, states, publish: next => publish(next) };
  };

  it('continues through the default selection to one Chrome launch and never past a retired attempt', async () => {
    const invoke = vi.fn(async name => name === 'desktop_chrome_available' ? true : undefined);
    vi.stubGlobal('__TAURI__', { core: { invoke } });
    try {
      const { subject, commands, opens, states, publish } = start();
      await subject.connect();
      await subject.act('account.connect', { recover_authority: true });
      expect(commands).toEqual([expect.objectContaining({ action: 'account.connect', recover_authority: true })]);
      publish({ ...base, revision: 1, authority_recovery: 'preparing', allowed_actions: [], pending_operations: [parent] });
      expect(commands).toHaveLength(1);
      publish(selection(2));
      await vi.waitFor(() => expect(commands).toHaveLength(2));
      expect(commands[1]).toMatchObject({ action: 'auth.select', provider: 'default', recover_authority: true });
      expect(authorityRecoveryView(states.at(-1)).action).toBeNull();
      publish(selection(3));
      publish({ ...base, revision: 4, authority_recovery: 'checking', allowed_actions: [], pending_operations: [parent] });
      publish(ready(5));
      await vi.waitFor(() => expect(opens).toHaveLength(1));
      expect(opens[0]).toMatchObject({ parent_operation_id: operationId, attempt_id: attempt.authentication_attempt_id });
      await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('desktop_browser_task', { entryCode: 'A'.repeat(43) }));
      publish(ready(6));
      publish({ ...ready(7), authority_recovery_browser: undefined, allowed_actions: ['auth.select'],
        authentication: auth('retired', { select: true }, attempt) });
      await vi.waitFor(() => expect(authorityRecoveryView(states.at(-1))).toMatchObject({ action: 'auth.select', label: 'Sign in' }));
      expect(commands).toHaveLength(2);
      expect(opens).toHaveLength(1);
      expect(subject.recoverySignIn).toBeNull();
      subject.stop();
    } finally { vi.unstubAllGlobals(); }
  });

  it('leaves each later step to an explicit click once its time bound has passed', async () => {
    let now = 1000;
    const { subject, commands, states, publish } = start(() => now);
    await subject.connect();
    await subject.act('account.connect', { recover_authority: true });
    now += 120_001;
    publish(selection(1));
    await Promise.resolve();
    expect(commands).toHaveLength(1);
    expect(subject.recoverySignIn).toBeNull();
    expect(authorityRecoveryView(states.at(-1))).toMatchObject({ action: 'auth.select', label: 'Sign in' });
    subject.stop();
  });

  it('ends the sequence on a reconnect instead of continuing an old click', async () => {
    const { subject, commands, publish } = start();
    await subject.connect();
    await subject.act('account.connect', { recover_authority: true });
    await subject.connect();
    publish(selection(1));
    await Promise.resolve();
    expect(commands).toHaveLength(1);
    expect(subject.recoverySignIn).toBeNull();
    subject.stop();
  });
});

describe('one-click provider sign-in across ceremony and source replacement', () => {
  const journey = '44444444-4444-4444-8444-444444444444';
  const otherJourney = '55555555-5555-4555-8555-555555555555';
  const existingParent = '22222222-2222-4222-8222-222222222222';
  const replaceOp = '33333333-3333-4333-8333-333333333333';
  const selectOp = '66666666-6666-4666-8666-666666666666';
  const otherParent = '77777777-7777-4777-8777-777777777777';
  const extraOp = '88888888-8888-4888-8888-888888888888';
  const verifiedChoices = { google: true, 'microsoft-personal': false, telegram: false, tiktok: false, default: true };
  const allFalse = { google: false, 'microsoft-personal': false, telegram: false, tiktok: false, default: false };
  const parentRecord = id => ({ ...record, operation_id: id, action: 'account.connect', state: 'waiting' });

  const startPath = (kind, { clock = () => 1000, saved = storage(), choices = verifiedChoices, withProjection = true } = {}) => {
    const parentId = kind === 'ceremony' ? existingParent : replaceOp;
    const base = kind === 'ceremony'
      ? { ...initial(), journey_id: journey, next_task: 'account-connection', allowed_actions: ['auth.select'],
          pending_operations: [parentRecord(existingParent)],
          authentication: { operation_id: existingParent, browser_operation_id: null, state: 'replacement-required',
            choices: withProjection ? choices : allFalse,
            actions: { select: true, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null } }
      : { ...initial(), journey_id: journey, next_task: 'account-connection', continuation_available: true,
          allowed_actions: ['account.connect'], pending_operations: [],
          authentication: withProjection
            ? { operation_id: null, browser_operation_id: null, state: 'replacement-required', choices,
                actions: { select: false, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null }
            : null };
    const replacementPending = revision => ({ ...base, revision, scope_digest: 'b'.repeat(64), continuation_available: false,
      allowed_actions: [],
      pending_operations: kind === 'ceremony'
        ? [parentRecord(existingParent), { ...record, operation_id: replaceOp, action: 'auth.select', state: 'waiting' }]
        : [{ ...record, operation_id: replaceOp, action: 'account.connect', state: 'waiting' }],
      authentication: { operation_id: parentId, browser_operation_id: null, state: 'replacement-pending',
        choices: allFalse, actions: { select: false, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null } });
    const adopting = revision => ({ ...base, revision, scope_digest: 'c'.repeat(64), continuation_available: false,
      allowed_actions: [], pending_operations: [parentRecord(parentId)], authentication: null });
    const successor = (revision, overrides = {}) => ({ ...base, revision, scope_digest: 'd'.repeat(64),
      continuation_available: false, allowed_actions: ['auth.select'],
      pending_operations: [parentRecord(overrides.parentId ?? parentId)],
      authentication: { operation_id: overrides.parentId ?? parentId, browser_operation_id: null, state: 'selection-required',
        choices: overrides.choices ?? verifiedChoices,
        actions: { select: true, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null },
      ...(overrides.journeyId ? { journey_id: overrides.journeyId } : {}) });

    let publish, latest = base;
    const ids = [replaceOp, selectOp, extraOp], commands = [], states = [];
    const openBrowser = vi.fn(async () => true);
    const subject = new DesktopSetupClient({ generation, storage: saved, receive: state => states.push(state),
      uuid: () => ids.shift(), now: clock, openBrowser,
      stream: async (_request, _headers, _signal, listener) => {
        publish = next => { latest = next; listener(JSON.stringify(next)); };
        listener(JSON.stringify(latest));
      },
      request: async (url, options) => {
        const body = options?.body && JSON.parse(options.body);
        if (url.endsWith('/session')) return response(session);
        if (url.endsWith('/commands')) {
          commands.push(body);
          return response({ ...record, operation_id: body.operation_id, action: body.action, state: 'accepted' }, 202);
        }
        const id = url.split('/').pop();
        return response({ ...record, operation_id: id, action: id === replaceOp && kind === 'source' ? 'account.connect' : 'auth.select', state: 'waiting' });
      } });
    const clickProvider = (provider = 'google') => kind === 'ceremony'
      ? subject.act('auth.select', { replace_ceremony: true }, { continueProvider: provider })
      : subject.act('account.connect', {}, { continueProvider: provider });
    return { subject, commands, states, openBrowser, saved, base, parentId, replacementPending, adopting, successor, clickProvider,
      publish: next => publish(next) };
  };

  describe.each(['ceremony', 'source'])('%s replacement path', kind => {
    it('renders verified predecessor provider buttons and keeps the clicked provider busy in place without layout shift', () => {
      document.documentElement.innerHTML = markup;
      const { base, parentId, replacementPending, successor } = startPath(kind);
      const busy = () => [...document.querySelectorAll('[data-provider]')].filter(button => button.getAttribute('aria-busy') === 'true')
        .map(button => button.dataset.provider);
      renderSetup(document, { snapshot: base, connected: true, pending: kind === 'ceremony' ? parentId : null });
      expect(document.getElementById('sign-in-options').hidden).toBe(false);
      expect(document.getElementById('task-action').hidden).toBe(true);
      expect(document.getElementById('browser-reopen').hidden).toBe(true);
      expect(document.getElementById('task-status').textContent).toBe('Sign-in ended.');
      expect(document.getElementById('task-status').dataset.tone).toBe('warning');
      expect(document.getElementById('task-status').dataset.reservedRegion).toBe('desktop-task-status');
      const google = document.querySelector('[data-provider="google"]');
      expect(google.hidden).toBe(false);
      expect(google.disabled).toBe(false);
      expect(google.dataset.action).toBe(kind === 'ceremony' ? 'auth.select' : 'account.connect');
      expect(google.dataset.replaceCeremony).toBe(String(kind === 'ceremony'));
      expect(google.dataset.sourceContinuation).toBe(String(kind === 'source'));
      expect(document.querySelector('[data-provider="microsoft-personal"]').hidden).toBe(true);
      expect(document.querySelector('[data-provider="default"]').hidden).toBe(false);

      // Clicked provider stays visible and busy in place through replacement-pending and follow-up selection.
      renderSetup(document, { snapshot: replacementPending(1), connected: true, pending: replaceOp, continuingProvider: 'google', provider: 'google' });
      expect(document.getElementById('sign-in-options').hidden).toBe(false);
      expect(google.hidden).toBe(false);
      expect(document.querySelector('[data-provider="microsoft-personal"]').hidden).toBe(true);
      expect(busy()).toEqual(['google']);
      expect([...document.querySelectorAll('[data-provider]:not([hidden])')].every(button => button.disabled)).toBe(true);
      expect(document.getElementById('task-status').textContent).toBe('Preparing sign-in\u2026');
      expect(document.getElementById('task-action').hidden).toBe(true);

      renderSetup(document, { snapshot: successor(2), connected: true, pending: selectOp, pendingAction: 'auth.select', provider: 'google' });
      expect(document.getElementById('sign-in-options').hidden).toBe(false);
      expect(busy()).toEqual(['google']);
      expect(document.getElementById('task-status').textContent).toBe('Finish signing in in your browser. This window continues by itself.');

      renderSetup(document, { snapshot: { ...successor(3), allowed_actions: [],
        authentication: { ...successor(3).authentication, state: 'selected', browser_operation_id: selectOp,
          actions: { select: false, cancel: true, open_browser: true, switch_account: false } } },
      connected: true, pending: selectOp, provider: 'google' });
      expect(document.getElementById('sign-in-options').hidden).toBe(true);
      expect(busy()).toEqual([]);
      expect(document.getElementById('task-status').textContent).toBe('Finish signing in in your browser. This window continues by itself.');
    });

    it('sends the replacement command then exactly one selection with the clicked provider and opens Chrome only through auth.select', async () => {
      const { subject, commands, states, openBrowser, replacementPending, adopting, successor, clickProvider, publish } = startPath(kind);
      await subject.connect();
      await clickProvider('google');
      expect(commands).toHaveLength(1);
      expect(commands[0]).toMatchObject(kind === 'ceremony'
        ? { action: 'auth.select', replace_ceremony: true, operation_id: replaceOp }
        : { action: 'account.connect', operation_id: replaceOp });
      expect(commands[0].provider).toBeUndefined();
      expect(openBrowser).not.toHaveBeenCalled();
      expect(states.at(-1).continuingProvider).toBe('google');

      publish(replacementPending(1));
      publish(adopting(2));
      expect(commands).toHaveLength(1);
      expect(openBrowser).not.toHaveBeenCalled();
      expect(states.at(-1).continuingProvider).toBe('google');

      publish(successor(3));
      await vi.waitFor(() => expect(commands).toHaveLength(2));
      expect(commands[1]).toEqual({
        profile: 'desktop-onboarding.v1', kind: 'command', journey_id: journey,
        operation_id: selectOp, action: 'auth.select', provider: 'google',
        expected: { generation, revision: 3, scope_digest: 'd'.repeat(64) },
      });
      await vi.waitFor(() => expect(openBrowser).toHaveBeenCalledTimes(1));
      expect(openBrowser.mock.calls[0][1]).toBe(selectOp);
      expect(subject.providerContinuation).toBeNull();
      expect(states.at(-1).continuingProvider).toBeNull();

      // Another selection-required snapshot never sends a second selection.
      publish(successor(4));
      await Promise.resolve();
      expect(commands).toHaveLength(2);
      expect(openBrowser).toHaveBeenCalledTimes(1);
      subject.stop();
    });

    it.each(['reconnect', 'another-command', 'changed-parent', 'changed-journey', 'provider-missing', 'time-bound'])(
      'ends the sequence with no selection sent on %s', async cause => {
        let now = 1000;
        const { subject, commands, states, openBrowser, successor, clickProvider, publish } = startPath(kind, { clock: () => now });
        await subject.connect();
        await clickProvider('google');
        expect(commands).toHaveLength(1);
        expect(states.at(-1).continuingProvider).toBe('google');
        expect(subject.providerContinuation).not.toBeNull();

        if (cause === 'reconnect') {
          await subject.connect();
          expect(subject.providerContinuation).toBeNull();
          publish(successor(1));
        } else if (cause === 'another-command') {
          subject.remember(null);
          publish({ ...initial(), journey_id: journey, revision: 1, next_task: 'account-connection',
            allowed_actions: ['browser.access'], pending_operations: [], authentication: null });
          await subject.act('browser.access');
          expect(subject.providerContinuation).toBeNull();
          publish(successor(2));
        } else if (cause === 'changed-parent') {
          publish(successor(1, { parentId: otherParent }));
        } else if (cause === 'changed-journey') {
          publish(successor(1, { journeyId: otherJourney }));
        } else if (cause === 'provider-missing') {
          publish(successor(1, { choices: { google: false, 'microsoft-personal': true, telegram: false, tiktok: false, default: true } }));
        } else if (cause === 'time-bound') {
          now += 120_001;
          publish(successor(1));
        }
        await Promise.resolve();
        expect(subject.providerContinuation).toBeNull();
        expect(states.at(-1).continuingProvider).toBeNull();
        const selections = commands.filter(item => item.action === 'auth.select' && item.provider !== undefined);
        expect(selections).toHaveLength(0);
        if (cause !== 'another-command') expect(openBrowser).not.toHaveBeenCalled();
        if (['provider-missing', 'time-bound'].includes(cause)) {
          document.documentElement.innerHTML = markup;
          renderSetup(document, { ...states.at(-1), pending: null });
          expect(document.getElementById('sign-in-options').hidden).toBe(false);
          const expectedButton = cause === 'provider-missing' ? 'microsoft-personal' : 'google';
          expect(document.querySelector(`[data-provider="${expectedButton}"]`).hidden).toBe(false);
          expect(document.querySelector(`[data-provider="${expectedButton}"]`).disabled).toBe(false);
          expect(document.querySelector(`[data-provider="${expectedButton}"]`).hasAttribute('aria-busy')).toBe(false);
        }
        subject.stop();
      });

    it('never continues a remembered provider after a restart and shows the successor buttons', async () => {
      const saved = storage();
      const first = startPath(kind, { saved });
      await first.subject.connect();
      await first.clickProvider('google');
      expect(first.states.at(-1).continuingProvider).toBe('google');
      expect(saved.getItem('desktop-setup-operation:' + generation)).toBe(replaceOp);
      first.subject.stop();

      // Reloaded client shares storage only; remembered provider is never persisted or sent.
      const reloadedCommands = [], reloadedStates = [];
      const reloadedOpen = vi.fn();
      let publishReloaded;
      const reopened = new DesktopSetupClient({ generation, storage: saved, receive: state => reloadedStates.push(state),
        uuid: () => selectOp, openBrowser: reloadedOpen,
        stream: async (_request, _headers, _signal, listener) => {
          publishReloaded = next => listener(JSON.stringify(next));
          publishReloaded(first.successor(2));
        },
        request: async (url, options) => {
          if (url.endsWith('/session')) return response(session);
          if (url.endsWith('/commands')) {
            reloadedCommands.push(JSON.parse(options.body));
            return response({ ...record, action: 'auth.select', state: 'accepted' }, 202);
          }
          return response({ ...record, operation_id: replaceOp, action: kind === 'ceremony' ? 'auth.select' : 'account.connect', state: 'completed' });
        } });
      await reopened.connect();
      await reopened.receipt();
      expect(reloadedCommands).toHaveLength(0);
      expect(reloadedOpen).not.toHaveBeenCalled();
      expect(reopened.providerContinuation).toBeNull();
      expect(reloadedStates.at(-1).continuingProvider).toBeNull();
      document.documentElement.innerHTML = markup;
      renderSetup(document, reloadedStates.at(-1));
      expect(document.getElementById('sign-in-options').hidden).toBe(false);
      expect(document.querySelector('[data-provider="google"]').disabled).toBe(false);
      expect(document.querySelector('[data-provider="google"]').hasAttribute('aria-busy')).toBe(false);
      reopened.stop();
    });

    it('keeps the single fallback button and refuses continuation when no verified predecessor projection exists', async () => {
      const { subject, commands, states, openBrowser, base, parentId, successor, clickProvider, publish } = startPath(kind, { withProjection: false });
      document.documentElement.innerHTML = markup;
      renderSetup(document, { snapshot: base, connected: true, pending: kind === 'ceremony' ? parentId : null });
      const action = document.getElementById('task-action');
      expect(action.hidden).toBe(false);
      expect(action.textContent).toBe(kind === 'ceremony' ? 'Sign in again' : 'Continue setup');
      expect(document.getElementById('sign-in-options').hidden).toBe(true);

      await subject.connect();
      await clickProvider('google');
      expect(commands).toHaveLength(1);
      expect(subject.providerContinuation).toBeNull();
      expect(states.at(-1)).toHaveProperty('continuingProvider', null);
      publish(successor(1));
      await Promise.resolve();
      expect(commands).toHaveLength(1);
      expect(openBrowser).not.toHaveBeenCalled();
      subject.stop();
    });
  });
});
