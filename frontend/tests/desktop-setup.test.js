import { afterEach, describe, expect, it, vi } from 'vitest';
import { runInNewContext } from 'node:vm';
import { readFileSync } from 'node:fs';
import generatedContract from '../onboarding/generated-contract.js?raw';
import setupThemeInitializer from '../public/setup-theme-init.js?raw';
import markup from '../onboarding/index.html?raw';
import { DesktopSetupClient } from '../onboarding/client.js';
import { snapshot } from '../onboarding/contract.js';
import { setupEvents } from '../onboarding/events.js';
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

it('selects the saved setup theme before the entry module can paint', () => {
  const earlyDocument = { documentElement: { dataset: {} } };
  runInNewContext(setupThemeInitializer, {document: earlyDocument, localStorage: {getItem: () => 'dark'}});
  expect(earlyDocument.documentElement.dataset.colorScheme).toBe('dark');
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
      browser_operation_id: operationId, state: 'selected', choices: { google: true, 'microsoft-personal': true, default: true },
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
          choices: { google: true, 'microsoft-personal': true, default: true },
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
            choices: { google: true, 'microsoft-personal': true, default: true },
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
          choices: { google: true, 'microsoft-personal': true, default: true },
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
      choices: { google: true, 'microsoft-personal': true, default: true },
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
  it.each(['google', 'microsoft-personal', 'default'])('retains the first %s click through preparation without selecting a replacement operation', async provider => {
    let publish, release;
    const authentication = { operation_id: null, browser_operation_id: null, state: 'selection-required',
      choices: { google: true, 'microsoft-personal': true, default: true }, authentication_attempt: null,
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
    const body = JSON.parse(request.mock.calls[0][1].body);
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
    const mutations = request.mock.calls.filter(([, options]) => options.method === 'POST');
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
    choices: { google: true, 'microsoft-personal': true, default: true },
    actions: { select: true, cancel: false, open_browser: false, switch_account: false }, authentication_attempt: null });

  it('renders only configured provider choices with the governed initial action', () => {
    mount();
    const value = { ...initial(), next_task: 'account-connection', allowed_actions: ['account.connect'],
      authentication: { ...signIn(), choices: { google: true, 'microsoft-personal': false, default: true } } };
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
        actions: { select: false, cancel: true, open_browser: true, switch_account: true } } };
    renderSetup(document, { snapshot: value, connected: true, pending: operationId });
    expect(document.getElementById('task-status').textContent).toBe('Finish signing in in Chrome.');
    expect(document.getElementById('sign-in-cancel')).toBeNull();
    expect(document.getElementById('browser-reopen').hidden).toBe(true);
    expect(document.getElementById('sign-in-switch').hidden).toBe(true);
    renderSetup(document, { snapshot: value, connected: true, error: 'browser_unavailable' });
    expect(document.getElementById('browser-reopen').hidden).toBe(false);
    expect(document.getElementById('browser-reopen').textContent).toBe('Try again');
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
      expect(document.getElementById('browser-reopen').textContent).toBe('Continue connecting');
      expect(document.getElementById('sign-in-options').hidden).toBe(true);
      expect(document.getElementById('task-action').hidden).toBe(true);
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

  it('distinguishes browser sign-in from accepted work without declaring local readiness', () => {
    mount();
    for (const [state, message] of [['awaiting-authentication', 'Finish signing in in Chrome.'],
      ['accepted', 'Account connection accepted. Finishing setup…']]) {
      renderSetup(document, { snapshot: { ...initial(), next_task: 'account-connection', allowed_actions: [],
        decision_status: { operation_id: operationId, state }, pending_operations: [{ ...record, action: 'account.connect' }] }, connected: true });
      expect(document.getElementById('task-status').textContent).toBe(message);
      expect(document.getElementById('task-action').hidden).toBe(true);
      expect(document.getElementById('open-dashboard')).toBeNull();
    }
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

  it('shows factual unavailable status and disables stale actions', () => {
    mount();
    renderSetup(document, { snapshot: initial(), connected: false, error: 'status_unavailable' });
    expect(document.querySelector('main').dataset.journeyState).toBe('desktop.setup_unavailable');
    expect(document.getElementById('task-action').disabled).toBe(true);
    expect(document.getElementById('task-reconnect').hidden).toBe(true);
    expect(document.getElementById('task-reconnect').getAttribute('aria-describedby')).toContain('task-status');
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
  it('requires explicit local authentication before pairing or opening the dashboard', () => {
    mount();
    const snapshot = { ...initial(), next_task: 'finish-setup', allowed_actions: ['browser.pair'] };
    for (const enrolled of [false, true]) {
      renderSetup(document, { snapshot, connected: true,
        authentication: { enrolled, checked: true, authenticated: false, busy: false } });
      expect(document.getElementById('local-access').hidden).toBe(false);
      expect(document.getElementById('local-access').dataset.journeyState).toBe('desktop.setup_local_access');
      expect(document.getElementById('local-authenticate').textContent).toBe(enrolled ? 'Sign in' : 'Set up access');
      expect(document.getElementById('task-action').disabled).toBe(true);
      expect(document.getElementById('open-dashboard')).toBeNull();
    }
    renderSetup(document, { snapshot, connected: true,
      authentication: { enrolled: true, checked: true, authenticated: true, busy: false } });
    expect(document.getElementById('local-access').hidden).toBe(true);
    expect(document.getElementById('task-action').disabled).toBe(false);
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
