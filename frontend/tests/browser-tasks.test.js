import { describe, it, expect, vi } from 'vitest';
import { payload, observation, id } from '../../extension/test-fixtures/browser-task.mjs';
import { connectExtension } from '../browser-tasks/extension.js';
import { BrowserTaskClient } from '../browser-tasks/client.js';
import { consumeLaunchIntent } from '../browser-tasks/launch-intent.js';
import { openBrowserTask } from '../onboarding/browser-task.js';
import { DesktopSetupClient } from '../onboarding/client.js';
import { helperGeneration, isPayload, isPayloadProjection, isBrowserTaskControl } from '../../shared/browser-tasks/wire.mjs';
import { renderBrowserTask } from '../browser-tasks/view.js';
import { readFileSync } from 'node:fs';
const event = () => { const listeners = []; return { listeners, addListener: fn => listeners.push(fn), emit: value => listeners.forEach(fn => fn(value)) }; };
const authenticationPayload = () => {
  const value = payload(); value.task.purpose = 'browser.authentication'; value.creator_account_id = 'synthetic-creator';
  value.ceremony = { entry_url: 'https://provider.example/onboarding/browser-ceremonies/' + 'A'.repeat(43),
    ceremony_id: '0199b234-5678-7000-8000-000000000002', attempt_id: 'A'.repeat(43),
    completion: { mode: 'extension-auth-flow', return_uri: `https://${value.extension_id}.chromiumapp.org/browser-ceremony/v1/complete` },
    expires_at: new Date(Date.now() + 30000).toISOString() };
  return value;
};

describe('scoped browser helper', () => {
  it.each(['ack', 'disconnect', 'timeout', 'foreign-ack'])('finishes only its admitted helper without rewriting completion on %s', async outcome => {
    vi.useFakeTimers();
    const value = payload(), receive = vi.fn();
    const port = { onMessage: event(), onDisconnect: event(), postMessage: vi.fn(), disconnect: vi.fn() };
    const link = connectExtension({ payload: value, receive, runtime: { connect: () => port } });
    try {
      port.onMessage.emit({ type: 'state', observation: observation() });
      receive.mockClear();
      const finished = link.finish();
      expect(port.postMessage).toHaveBeenLastCalledWith({ type: 'finish', task_id: value.task.task_id, admission_generation: value.task.admission_generation });
      port.onMessage.emit({ type: 'state', observation: observation() });
      expect(port.disconnect).not.toHaveBeenCalled();
      if (outcome === 'disconnect') port.onDisconnect.emit();
      else if (outcome === 'timeout') await vi.advanceTimersByTimeAsync(5000);
      else port.onMessage.emit({ type: 'finished', task_id: outcome === 'foreign-ack' ? id(99) : value.task.task_id,
        admission_generation: value.task.admission_generation });
      expect(await finished).toBe(outcome === 'ack');
      expect(receive).not.toHaveBeenCalled();
      expect(port.disconnect).toHaveBeenCalledOnce();
    } finally { link.close(); vi.useRealTimers(); }
  });
  it('reports window closure once through its scoped port without claiming Provider cancellation', async () => {
    const value = authenticationPayload(), received = [], authenticate = vi.fn(), finish = vi.fn(async () => true);
    let publish;
    const request = vi.fn(async path => ({ ok: true, text: async () => JSON.stringify(path.endsWith('/session')
      ? { role: 'browser_task', generation: value.task.admission_generation, task_id: value.task.task_id,
        journey_id: value.task.journey_id, csrf_token: 'A'.repeat(43) }
      : path.endsWith('/payload') ? value : { state: 'checking' }) }));
    const client = new BrowserTaskClient({ generation: value.task.admission_generation, request,
      receive: state => received.push(state), connect: ({ receive }) => { publish = receive; return { authenticate, finish, close: vi.fn() }; } });
    await client.start();
    publish({ state: 'auth_closed', notification: null }); await client.serial;
    publish({ state: 'auth_closed', notification: null }); await client.serial;
    const posts = request.mock.calls.filter(([, options]) => options.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toBe('/api/v1/browser-tasks/closed');
    expect(JSON.parse(posts[0][1].body)).toEqual({});
    expect(received.at(-1).state).toBe('auth_closed');
    expect(received.some(item => item.state === 'task_completed')).toBe(false);
    expect(value.task.state).toBe('accepted');
    expect(finish).toHaveBeenCalledOnce();
    expect(authenticate).not.toHaveBeenCalled(); client.stop();
  });
  it.each(['ack', 'lost-reply', 'rejected', 'malformed', 'pagehide', 'replacement'])(
    'finishes only its own helper after durable closure acknowledgement: %s', async outcome => {
      vi.useFakeTimers();
      const value = authenticationPayload(), receive = vi.fn(), finish = vi.fn(async () => true);
      let publish, settle;
      const request = vi.fn(async path => {
        if (path.endsWith('/closed')) return new Promise((resolve, reject) => { settle = { resolve, reject }; });
        return { ok: true, text: async () => JSON.stringify(path.endsWith('/session')
          ? { role: 'browser_task', generation: value.task.admission_generation, task_id: value.task.task_id,
            journey_id: value.task.journey_id, csrf_token: 'A'.repeat(43) } : value) };
      });
      const client = new BrowserTaskClient({ generation: value.task.admission_generation, request, receive,
        connect: ({ receive: send }) => { publish = send; return { finish, close: vi.fn() }; } });
      try {
        await client.start(); publish({ state: 'auth_closed', notification: null });
        await Promise.resolve(); await Promise.resolve();
        expect(finish).not.toHaveBeenCalled();
        expect(receive.mock.calls.at(-1)[0].state).toBe('auth_checking');
        if (outcome === 'pagehide') client.stop();
        if (outcome === 'replacement') await client.start();
        if (outcome === 'lost-reply') settle.reject(Error('network'));
        else settle.resolve({ ok: outcome !== 'rejected', status: 409,
          text: async () => JSON.stringify(outcome === 'malformed' ? { state: 'completed' } : { state: 'checking' }) });
        await client.serial;
        expect(finish).toHaveBeenCalledTimes(outcome === 'ack' ? 1 : 0);
        expect(receive.mock.calls.some(([item]) => item.state === 'task_completed')).toBe(false);
        if (outcome === 'ack') {
          const reads = request.mock.calls.length;
          expect(receive.mock.calls.at(-1)[0].state).toBe('auth_closed');
          await vi.advanceTimersByTimeAsync(5000);
          expect(request).toHaveBeenCalledTimes(reads);
        } else if (['lost-reply', 'rejected', 'malformed'].includes(outcome)) {
          expect(receive.mock.calls.at(-1)[0].state).toBe('service_unavailable');
        }
      } finally { client.stop(); vi.useRealTimers(); }
    });
  it('does not treat authentication closure as pairing completion', async () => {
    const value = payload(), finish = vi.fn(), receive = vi.fn();
    value.task.purpose = 'browser.pairing-confirmation';
    let publish;
    const request = vi.fn(async path => ({ ok: true, text: async () => JSON.stringify(path.endsWith('/session')
      ? { role: 'browser_task', generation: value.task.admission_generation, task_id: value.task.task_id,
        journey_id: value.task.journey_id, csrf_token: 'A'.repeat(43) } : value) }));
    const client = new BrowserTaskClient({ generation: value.task.admission_generation, request, receive,
      connect: ({ receive: send }) => { publish = send; return { finish, close: vi.fn() }; } });
    try {
      await client.start(); publish({ state: 'auth_closed', notification: null }); await client.serial;
      expect(finish).not.toHaveBeenCalled();
      expect(request.mock.calls.some(([, options]) => options.method === 'POST')).toBe(false);
      expect(receive.mock.calls.some(([item]) => item.state === 'auth_closed')).toBe(false);
    } finally { client.stop(); }
  });
  it('renders acknowledged window closure without claiming domain completion', () => {
    document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
    const value = authenticationPayload();
    renderBrowserTask(document, { state: 'auth_closed', payload: value });
    expect(document.querySelector('main').dataset.journeyState).toBe('browser.auth_closed');
    expect(document.getElementById('task-status').textContent.length).toBeGreaterThan(10);
    expect(document.getElementById('authentication-open').hidden).toBe(true);
    expect(document.getElementById('desktop-return').hidden).toBe(false);
    expect(document.getElementById('desktop-return').getAttribute('href')).toBe('ofca://onboarding/?journey=' + value.task.journey_id);
  });
  it.each(['tasks', 'entry', 'dispatch'])('fences a delayed %s reply after the native launch owner stops', async stage => {
    const value = authenticationPayload(), invoke = vi.fn();
    let release;
    const json = vi.fn(async path => {
      if (stage === 'tasks' && path.endsWith('/browser-tasks') || path.endsWith('/' + stage)) await new Promise(resolve => { release = resolve; });
      return path.endsWith('/entry') ? { entry_code: 'A'.repeat(43) }
        : path.endsWith('/dispatch') ? { state: 'unreachable' } : { tasks: [value.task] };
    });
    const client = new DesktopSetupClient({ generation: id(4), receive: vi.fn(), storage: {},
      openBrowser: (owner, operation, _invoke, current) => openBrowserTask(owner, operation, invoke, current) });
    client.json = json; client.connected = true; client.session = { stage: stage === 'dispatch' ? 'runtime' : 'setup' };
    client.launchOperation = value.task.operation_id;
    const pending = client.launchBrowser();
    await Promise.resolve(); await Promise.resolve();
    client.stop(); release(); await pending;
    expect(invoke).not.toHaveBeenCalled();
    expect(json.mock.calls.some(([path]) => path.endsWith('/entry'))).toBe(stage === 'entry');
  });
  it.each(['navigate', 'reload', 'back_forward', 'missing', 'mismatch', 'get-failure', 'remove-failure'])(
    'spends native entry intent once and refuses %s recovery', type => {
      const saved = new Map([['browser-task-launch', type === 'mismatch' ? id(9) : id(4)]]);
      const storage = { getItem: vi.fn(key => { if (type === 'get-failure') throw Error('unavailable'); return saved.get(key); }),
        removeItem: vi.fn(key => { if (type === 'remove-failure') throw Error('unavailable'); saved.delete(key); }) };
      const navigation = type === 'missing' ? [] : [{ type: type.endsWith('failure') || type === 'mismatch' ? 'navigate' : type }];
      expect(consumeLaunchIntent(id(4), storage, navigation)).toBe(type === 'navigate');
      expect(consumeLaunchIntent(id(4), storage, navigation)).toBe(false);
      if (type !== 'get-failure') expect(storage.removeItem).toHaveBeenCalled();
    });
  it('continues a native sign-in choice once, only after exact idle and a fresh bound payload', async () => {
    const value = authenticationPayload(), authenticate = vi.fn(() => true), received = [];
    let publish;
    const request = vi.fn(async path => ({ ok: true, text: async () => JSON.stringify(path.endsWith('/session')
      ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) } : value) }));
    const client = new BrowserTaskClient({ generation: id(4), nativeLaunch: true, request, receive: next => received.push(next),
      connect: ({ receive }) => { publish = receive; return { close: vi.fn(), authenticate }; } });
    await client.start();
    publish({ state: 'ready' }); await client.serial;
    expect(authenticate).not.toHaveBeenCalled();
    publish({ state: 'auth_idle', notification: null });
    expect(received.at(-1).state).toBe('auth_opening');
    publish({ state: 'auth_idle', notification: null }); await client.serial;
    expect(authenticate).toHaveBeenCalledOnce();
    expect(request.mock.calls.filter(([path]) => path.endsWith('/payload'))).toHaveLength(2);
    await client.start(); publish({ state: 'auth_idle', notification: null }); await client.serial;
    expect(authenticate).toHaveBeenCalledOnce();
    client.stop();
  });
  it.each(['opening', 'unknown', 'disconnect', 'pagehide', 'operation', 'ceremony', 'expiry', 'request-failure'])(
    'does not dispatch queued native sign-in after %s', async reason => {
      const value = authenticationPayload(), authenticate = vi.fn(() => true);
      let publish, release, reads = 0;
      const request = vi.fn(async path => {
        if (path.endsWith('/payload') && ++reads === 2) await new Promise(resolve => { release = resolve; });
        if (reads >= 2 && reason === 'request-failure') throw Error('unavailable');
        return { ok: true, text: async () => JSON.stringify(path.endsWith('/session')
          ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) } : value) };
      });
      const client = new BrowserTaskClient({ generation: id(4), nativeLaunch: true, request, receive: vi.fn(),
        connect: ({ receive }) => { publish = receive; return { close: vi.fn(), authenticate }; } });
      await client.start(); publish({ state: 'auth_idle', notification: null });
      await Promise.resolve();
      if (reason === 'opening') publish({ state: 'auth_opening', notification: null });
      if (reason === 'unknown') publish({ state: 'auth_checking', notification: { ...value.ceremony, notification: 'unavailable' } });
      if (reason === 'disconnect') publish({ state: 'unavailable' });
      if (reason === 'pagehide') client.stop();
      if (reason === 'operation') value.task.operation_id = id(99);
      if (reason === 'ceremony') value.ceremony.attempt_id = 'B'.repeat(43);
      if (reason === 'expiry') client.now = () => Date.parse(value.ceremony.expires_at) + 1;
      release(); await client.serial;
      expect(authenticate).not.toHaveBeenCalled();
      publish({ state: 'auth_idle', notification: null }); await client.serial;
      expect(authenticate).not.toHaveBeenCalled();
      client.stop();
    });
  it('renders task status and recovery without a second setup wizard', () => {
    document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
    for (const state of ['loading', 'unavailable', 'profile_mismatch', 'account_mismatch', 'task_expired', 'task_changed', 'service_unavailable',
      'needs-consent', 'needs-permission', 'needs-account', 'ready', 'paused', 'reported', 'task_completed']) {
      renderBrowserTask(document, { state, payload: payload() });
      expect(document.querySelector('main').dataset.journeyState).toBe('browser.' + state.replaceAll('-', '_'));
      expect(document.getElementById('task-status').textContent.length).toBeGreaterThan(10);
      expect(document.querySelector('nav')).toBeNull();
      if (['task_expired', 'task_changed', 'profile_mismatch', 'task_completed'].includes(state)) {
        expect(document.getElementById('desktop-return').hidden).toBe(false);
        expect(document.getElementById('desktop-return').getAttribute('href')).toBe('ofca://onboarding/?journey=' + payload().task.journey_id);
        expect(document.getElementById('extension-open').hidden).toBe(true);
      }
    }
  });
  it('treats missing or disabled discovery as unavailable', async () => {
    for (const runtime of [null, { connect() { throw Error('disabled'); } }]) {
      const receive = vi.fn();
      const link = connectExtension({ payload: payload(), runtime, receive });
      await Promise.resolve();
      expect(receive).toHaveBeenCalledWith({ state: 'unavailable' });
      link.close();
    }
  });
  it('routes to the pinned extension and refuses wrong task, instance and revision messages', () => {
    const value = payload(), receive = vi.fn();
    const port = { onMessage: event(), onDisconnect: event(), postMessage: vi.fn(), disconnect: vi.fn() };
    const runtime = { connect: vi.fn(() => port) };
    const link = connectExtension({ payload: value, runtime, receive });
    expect(runtime.connect).toHaveBeenCalledWith(value.extension_id, { name: 'ofca.browser-task.v1' });
    port.onMessage.emit({ type: 'state', observation: observation(value.task) });
    expect(receive.mock.calls.at(-1)[0].state).toBe('needs-consent');
    expect(link.open()).toBe(true);
    port.onMessage.emit({ type: 'state', observation: { ...observation(value.task), instance_id: id(90), revision: 2 } });
    expect(receive.mock.calls.at(-1)[0].state).toBe('unavailable');
    expect(link.open()).toBe(false); expect(port.disconnect).toHaveBeenCalledOnce();
  });
  it('does not deliver intermediate consent decisions to Brain and reports only the admitted account observation', async () => {
    const value = payload(), received = [], requests = [];
    let publish;
    const request = vi.fn(async (path, options) => {
      requests.push([path, options]);
      const body = path.endsWith('/session') ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) }
        : path.endsWith('/payload') ? value : { state: 'observed' };
      return { ok: true, text: async () => JSON.stringify(body) };
    });
    const client = new BrowserTaskClient({ generation: id(4), request, receive: next => received.push(next),
      connect: ({ receive }) => { publish = receive; return { close: vi.fn() }; } });
    await client.start();
    publish({ state: 'needs-consent', observation: observation() });
    await client.serial; expect(requests).toHaveLength(2);
    const ready = { ...observation(), state: 'ready', creator_account_id: 'synthetic-creator' };
    publish({ state: 'ready', observation: ready }); await client.serial;
    expect(JSON.parse(requests[2][1].body)).toEqual(ready);
    expect(requests[2][1].headers['X-Desktop-Generation']).toBe(id(4));
    expect(received.at(-1).state).toBe('reported'); client.stop();
  });
  it('opens only one prepared task through the native code-only command', async () => {
    const value = payload(), invoke = vi.fn();
    const client = { json: vi.fn(async path => path.endsWith('/entry') ? { entry_code: 'A'.repeat(43) } : { tasks: [value.task] }) };
    expect(await openBrowserTask(client, value.task.operation_id, invoke)).toBe(true);
    expect(invoke).toHaveBeenCalledExactlyOnceWith('A'.repeat(43));
    client.json.mockResolvedValue({ tasks: [value.task, value.task] });
    await expect(openBrowserTask(client, value.task.operation_id, invoke)).rejects.toThrow();
    expect(invoke).toHaveBeenCalledOnce();
  });
  it('keeps a delivered browser observation complete after a disconnect or its old timeout', async () => {
    vi.useFakeTimers();
    try {
      const value = payload(), received = [], close = vi.fn();
      let publish, acknowledge;
      const request = vi.fn(async path => {
        if (path.endsWith('/observation')) await new Promise(resolve => { acknowledge = resolve; });
        const body = path.endsWith('/session') ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) }
          : path.endsWith('/payload') ? value : { state: 'observed' };
        return { ok: true, text: async () => JSON.stringify(body) };
      });
      const client = new BrowserTaskClient({ generation: id(4), request, receive: next => received.push(next),
        connect: ({ receive }) => { publish = receive; return { close }; } });
      await client.start();
      publish({ state: 'ready', observation: { ...observation(), state: 'ready', creator_account_id: 'synthetic-creator' } });
      await Promise.resolve();
      publish({ state: 'unavailable' });
      acknowledge(); await client.serial;
      expect(received.at(-1).state).toBe('reported');
      expect(close).toHaveBeenCalledOnce();
      const count = received.length;
      publish({ state: 'unavailable' });
      await vi.advanceTimersByTimeAsync(60000);
      expect(received).toHaveLength(count);
      expect(request).toHaveBeenCalledTimes(3);
      client.stop();
    } finally { vi.useRealTimers(); }
  });
  it.each([false, true])('reads a completed task without connecting again and rejects replaced admission: %s', async replaced => {
    const value = payload(), received = [], connect = vi.fn();
    value.task.state = 'completed';
    if (replaced) value.task.admission_generation = id(99);
    const request = vi.fn(async path => ({ ok: true, text: async () => JSON.stringify(path.endsWith('/session')
      ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) } : value) }));
    const client = new BrowserTaskClient({ generation: id(4), request, connect, receive: next => received.push(next) });
    await client.start();
    expect(received.at(-1).state).toBe(replaced ? 'task_changed' : 'task_completed');
    expect(connect).not.toHaveBeenCalled();
    client.stop();
  });
  it('reads completed domain status before blaming a dropped extension connection', async () => {
    const value = payload(), received = [], close = vi.fn();
    let publish, completed = false;
    const request = vi.fn(async path => ({ ok: true, text: async () => JSON.stringify(path.endsWith('/session')
      ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) }
      : { ...value, task: { ...value.task, state: completed ? 'completed' : 'accepted' } }) }));
    const client = new BrowserTaskClient({ generation: id(4), request, receive: next => received.push(next),
      connect: ({ receive }) => { publish = receive; return { close }; } });
    await client.start();
    completed = true;
    publish({ state: 'unavailable' }); await client.serial;
    expect(received.at(-1).state).toBe('task_completed');
    expect(close).toHaveBeenCalledOnce();
    expect(request.mock.calls.every(([, options]) => options.method === undefined)).toBe(true);
    client.stop();
  });
  it.each(['start', 'poll', 'disconnect'])('accepts the completed authentication projection on %s without launch authority', async source => {
    vi.useFakeTimers();
    const value = authenticationPayload(), received = [], close = vi.fn(), authenticate = vi.fn();
    let completed = source === 'start', publish;
    const projection = () => completed ? { ...value, task: { ...value.task, state: 'completed' }, ceremony: null } : value;
    const request = vi.fn(async path => ({ ok: true, text: async () => JSON.stringify(path.endsWith('/session')
      ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) } : projection()) }));
    const connect = vi.fn(({ receive }) => { publish = receive; return { close, authenticate }; });
    const client = new BrowserTaskClient({ generation: id(4), request, connect, receive: next => received.push(next) });
    try {
      await client.start(); completed = true;
      expect(isPayloadProjection(projection())).toBe(true);
      expect(isPayload(projection())).toBe(false);
      expect(isBrowserTaskControl({ type: 'session.control', id: id(1), action: 'browser.task', payload: projection() })).toBe(false);
      if (source === 'poll') await vi.advanceTimersByTimeAsync(2000);
      if (source === 'disconnect') { publish({ state: 'unavailable' }); await client.serial; }
      expect(received.at(-1).state).toBe('task_completed');
      document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
      renderBrowserTask(document, received.at(-1));
      expect(document.getElementById('authentication-browser').hasAttribute('href')).toBe(false);
      expect(connect).toHaveBeenCalledTimes(source === 'start' ? 0 : 1);
      expect(authenticate).not.toHaveBeenCalled();
      expect(request.mock.calls.every(([, options]) => options.method === undefined)).toBe(true);
    } finally { client.stop(); vi.useRealTimers(); }
  });
  it.each(['task_id', 'journey_id', 'operation_id', 'scope_digest', 'source_revision', 'admission_generation', 'expires_at', 'creator_account_id'])(
    'refuses a completed authentication projection with changed %s', async field => {
      const value = authenticationPayload();
      const current = structuredClone(value); current.task.state = 'completed'; current.ceremony = null;
      if (field === 'creator_account_id') current[field] = 'foreign-creator';
      else current.task[field] = field === 'scope_digest' ? 'b'.repeat(64)
        : ['source_revision', 'expires_at'].includes(field) ? current.task[field] + 1 : id(99);
      const client = new BrowserTaskClient({ generation: id(4), receive: vi.fn(), request: async () => ({ ok: true, text: async () => JSON.stringify(current) }) });
      client.payload = value;
      await expect(client.currentPayload()).rejects.toThrow('task_changed');
      expect(isPayloadProjection({ ...value, ceremony: null })).toBe(false);
    });
  it.each(['dispatched', 'unreachable', 'unconfirmed'])('routes a paired task with %s delivery without duplicating uncertain work', async (state) => {
    const value = authenticationPayload(), invoke = vi.fn();
    const client = { session: { stage: 'runtime' }, json: vi.fn(async path => path.endsWith('/dispatch')
      ? { state } : path.endsWith('/entry') ? { entry_code: 'A'.repeat(43) } : { tasks: [value.task] }) };
    if (state === 'unconfirmed') await expect(openBrowserTask(client, value.task.operation_id, invoke)).rejects.toThrow('browser_unavailable');
    else expect(await openBrowserTask(client, value.task.operation_id, invoke)).toBe(true);
    expect(client.json.mock.calls.some(([path]) => path.endsWith('/entry'))).toBe(state === 'unreachable');
    expect(invoke).toHaveBeenCalledTimes(state === 'unreachable' ? 1 : 0);
  });
  it('renders focused authentication and ordinary Chrome recovery without declaring success', () => {
    document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
    const value = authenticationPayload();
    for (const state of ['auth_idle', 'auth_opening', 'auth_checking', 'auth_unconfirmed', 'auth_access_required', 'unavailable']) {
      renderBrowserTask(document, { state, payload: value });
      expect(document.querySelector('main').dataset.journeyState).toBe('browser.' + state);
      expect(document.getElementById('authentication-open').hidden).toBe(!['auth_idle', 'auth_unconfirmed'].includes(state));
      expect(document.getElementById('authentication-browser').hidden).toBe(state !== 'unavailable');
      expect(document.getElementById('extension-open').hidden).toBe(true);
    }
    expect(document.getElementById('authentication-browser').href).toBe(value.ceremony.entry_url + '#system-browser');
    expect(document.getElementById('desktop-return').hidden).toBe(false);
  });
  it('forwards only exact authentication correlation and launches only after an explicit action', () => {
    for (const invalid of [false, true]) {
      const value = authenticationPayload(), receive = vi.fn();
      const port = { onMessage: event(), onDisconnect: event(), postMessage: vi.fn(), disconnect: vi.fn() };
      const link = connectExtension({ payload: value, runtime: { connect: () => port }, receive });
      port.onMessage.emit({ type: 'state', observation: { ...observation(value.task), state: 'ready', creator_account_id: value.creator_account_id } });
      expect(port.postMessage).toHaveBeenCalledTimes(1);
      expect(link.authenticate()).toBe(true);
      expect(port.postMessage).toHaveBeenLastCalledWith({ type: 'authenticate', task_id: value.task.task_id, admission_generation: value.task.admission_generation });
      const notification = { ceremony_id: value.ceremony.ceremony_id, attempt_id: value.ceremony.attempt_id, notification: 'changed' };
      if (invalid) notification.attempt_id = 'B'.repeat(43);
      port.onMessage.emit({ type: 'authentication', task_id: value.task.task_id, admission_generation: value.task.admission_generation,
        state: 'checking', notification });
      expect(receive.mock.calls.at(-1)[0].state).toBe(invalid ? 'unavailable' : 'auth_checking');
      link.close();
    }
  });
  it('notifies Brain to read an uncertain authentication outcome without retrying its operation', async () => {
    const value = authenticationPayload(), received = [], authenticate = vi.fn(() => true);
    let publish;
    const request = vi.fn(async path => {
      const body = path.endsWith('/session') ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) }
        : path.endsWith('/payload') ? value : { state: 'checking' };
      return { ok: true, text: async () => JSON.stringify(body) };
    });
    const client = new BrowserTaskClient({ generation: id(4), request, receive: state => received.push(state),
      connect: ({ receive }) => { publish = receive; return { close: vi.fn(), authenticate }; } });
    await client.start();
    publish({ state: 'ready', observation: { ...observation(value.task), state: 'ready', creator_account_id: value.creator_account_id } });
    publish({ state: 'auth_idle', notification: null });
    expect(authenticate).not.toHaveBeenCalled();
    expect(await client.authenticate()).toBe(true);
    const notification = { ceremony_id: value.ceremony.ceremony_id, attempt_id: value.ceremony.attempt_id, notification: 'unavailable' };
    publish({ state: 'auth_checking', notification }); await client.serial;
    expect(received.at(-1).state).toBe('auth_unconfirmed');
    publish({ state: 'auth_checking', notification }); await client.serial;
    expect(received.at(-1).state).toBe('auth_unconfirmed');
    const mutations = request.mock.calls.filter(([, options]) => options.method === 'POST');
    expect(mutations).toHaveLength(1);
    expect(mutations[0][0]).toBe('/api/v1/browser-tasks/notification');
    expect(JSON.parse(mutations[0][1].body)).toEqual(notification);
    expect(authenticate).toHaveBeenCalledOnce(); client.stop();
  });
  it('keeps an admitted port live and stops after a missing acknowledgement', async () => {
    vi.useFakeTimers();
    try {
      const value = payload(), receive = vi.fn();
      const port = { onMessage: event(), onDisconnect: event(), postMessage: vi.fn(), disconnect: vi.fn() };
      const link = connectExtension({ payload: value, runtime: { connect: () => port }, receive });
      port.onMessage.emit({ type: 'state', observation: observation(value.task) });
      await vi.advanceTimersByTimeAsync(20000);
      expect(port.postMessage).toHaveBeenLastCalledWith({ type: 'ping', task_id: value.task.task_id, admission_generation: value.task.admission_generation });
      port.onMessage.emit({ type: 'pong', task_id: value.task.task_id, admission_generation: value.task.admission_generation });
      await vi.advanceTimersByTimeAsync(20000);
      expect(receive.mock.calls.at(-1)[0].state).toBe('needs-consent');
      await vi.advanceTimersByTimeAsync(5000);
      expect(receive).toHaveBeenLastCalledWith({ state: 'unavailable' });
      expect(link.open()).toBe(false);
      const count = port.postMessage.mock.calls.length;
      await vi.advanceTimersByTimeAsync(40000);
      expect(port.postMessage.mock.calls.length).toBe(count);
      link.close();
    } finally { vi.useRealTimers(); }
  });
  it('reconciles a dropped port refusal and keeps the desktop return locator on retry', async () => {
    const value = payload(), received = [];
    let publish, retired = false;
    const request = vi.fn(async path => {
      if (retired) return { ok: false, status: 409 };
      const body = path.endsWith('/session') ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) } : value;
      return { ok: true, text: async () => JSON.stringify(body) };
    });
    const client = new BrowserTaskClient({ generation: id(4), request, receive: next => received.push(next),
      connect: ({ receive }) => { publish = receive; return { close: vi.fn() }; } });
    await client.start();
    publish({ state: 'unavailable' }); await client.serial;
    expect(received.at(-1)).toEqual({ state: 'unavailable', payload: value });
    retired = true;
    publish({ state: 'unavailable' }); await client.serial;
    expect(received.at(-1)).toEqual({ state: 'task_changed', payload: value });
    expect(request.mock.calls.every(([, options]) => options.method === undefined)).toBe(true);
    await client.start();
    expect(received.at(-1)).toEqual({ state: 'task_changed', payload: value });
    client.stop();
  });
  it('requires an exact helper path and canonical presentation fragment', () => {
    const url = 'http://bridge.localhost:17871/browser-tasks#presentation=' + id(4);
    expect(helperGeneration(url)).toBe(id(4));
    for (const invalid of [url + '&extra=1', url.replace('/browser-tasks', '/'), url.replace('http:', 'https:'), url.replace('bridge.localhost', '127.0.0.1')]) expect(helperGeneration(invalid)).toBeNull();
  });
  it('accepts the independent pairing code only from the exact pairing task port', () => {
    for (const changed of [false, true]) {
      const value = payload(); value.task.purpose = 'browser.pairing-confirmation'; value.creator_account_id = 'synthetic-creator';
      const receive = vi.fn(), port = { onMessage: event(), onDisconnect: event(), postMessage: vi.fn(), disconnect: vi.fn() };
      const link = connectExtension({ payload: value, receive, runtime: { connect: () => port } });
      expect(link.pair()).toBe(false);
      port.onMessage.emit({ type: 'state', observation: { ...observation(), state: 'ready', creator_account_id: value.creator_account_id } });
      expect(link.pair()).toBe(true);
      port.onMessage.emit({ type: 'pairing', state: 'compare', task_id: changed ? id(99) : value.task.task_id,
        admission_generation: value.task.admission_generation, comparison_code: '042917' });
      expect(receive.mock.calls.at(-1)[0]).toEqual(changed ? { state: 'unavailable' }
        : { state: 'pairing_compare', comparison_code: '042917' });
      link.close();
    }
  });
  it.each(['exact', 'task', 'generation', 'extra', 'purpose'])('accepts only the %s pairing completion receipt', kind => {
    const value = payload(); value.task.purpose = kind === 'purpose' ? 'browser.access' : 'browser.pairing-confirmation';
    const receive = vi.fn(), port = { onMessage: event(), onDisconnect: event(), postMessage: vi.fn(), disconnect: vi.fn() };
    const link = connectExtension({ payload: value, receive, runtime: { connect: () => port } });
    port.onMessage.emit({ type: 'state', observation: observation() });
    port.onMessage.emit({ type: 'pairing-completed', task_id: kind === 'task' ? id(99) : value.task.task_id,
      admission_generation: kind === 'generation' ? id(99) : value.task.admission_generation,
      ...(kind === 'extra' ? { paired: true } : {}) });
    expect(receive).toHaveBeenLastCalledWith({ state: kind === 'exact' ? 'pairing_completed' : 'unavailable' });
    link.close();
  });
  it.each(['brain-first', 'extension-first', 'lost-comparison-reply', 'replace', 'expired'])(
    'keeps its original pairing port until both owners finish: %s', async order => {
      vi.useFakeTimers();
      const value = payload(); value.task.purpose = 'browser.pairing-confirmation'; value.creator_account_id = 'synthetic-creator';
      value.task.expires_at = (Date.now() + 10000) / 1000;
      let publish, completed = false;
      const close = vi.fn(), pair = vi.fn(() => true), replacePairing = vi.fn(() => true), received = [];
      const request = vi.fn(async path => {
        if (path.endsWith('/pairing') && order === 'lost-comparison-reply') throw Error('lost after commit');
        return { ok: true, text: async () => JSON.stringify(path.endsWith('/session')
          ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) }
          : path.endsWith('/payload') ? { ...value, task: { ...value.task, state: completed ? 'completed' : 'accepted' } }
            : { state: 'checking' }) };
      });
      const client = new BrowserTaskClient({ generation: id(4), request, receive: next => received.push(next),
        connect: ({ receive }) => { publish = receive; return { close, pair, replacePairing }; } });
      try {
        await client.start();
        if (order === 'replace') { publish({ state: 'pairing_saved' }); await client.replacePairing(); }
        else { publish({ state: 'ready' }); await client.serial; }
        publish({ state: 'pairing_compare', comparison_code: '042917' }); await client.serial;
        if (order === 'extension-first') {
          publish({ state: 'pairing_completed' }); await client.serial;
          expect(received.at(-1).state).toBe('pairing_checking');
          expect(close).not.toHaveBeenCalled();
        }
        completed = true;
        await vi.advanceTimersByTimeAsync(2000);
        if (order !== 'extension-first') {
          expect(close).not.toHaveBeenCalled();
          expect(received.at(-1).state).toBe('pairing_checking');
          if (order === 'expired') await vi.advanceTimersByTimeAsync(8000);
          else { publish({ state: 'pairing_completed' }); await client.serial; }
        }
        expect(close).toHaveBeenCalledOnce();
        expect(received.at(-1).state).toBe(order === 'expired' ? 'task_expired' : 'task_completed');
        expect(pair).toHaveBeenCalledTimes(order === 'replace' ? 0 : 1);
        expect(replacePairing).toHaveBeenCalledTimes(order === 'replace' ? 1 : 0);
        expect(request.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
      } finally { client.stop(); vi.useRealTimers(); }
    });
  it.each([false, true])('forwards one pairing code and preserves a lost comparison reply: %s', async lost => {
    const value = payload(); value.task.purpose = 'browser.pairing-confirmation'; value.creator_account_id = 'synthetic-creator';
    let publish;
    const received = [], pair = vi.fn(() => true), close = vi.fn();
    const request = vi.fn(async (path, options) => {
      if (path.endsWith('/pairing')) {
        expect(JSON.parse(options.body)).toEqual({ comparison_code: '042917' });
        if (lost) throw Error('lost after commit');
      }
      return { ok: true, text: async () => JSON.stringify(path.endsWith('/session')
        ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) }
        : path.endsWith('/payload') ? value : { state: 'checking' }) };
    });
    const client = new BrowserTaskClient({ generation: id(4), request, receive: next => received.push(next),
      connect: ({ receive }) => { publish = receive; return { pair, close }; } });
    await client.start();
    expect(pair).not.toHaveBeenCalled();
    publish({ state: 'needs-permission' });
    expect(received.at(-1).state).toBe('pairing_access_required');
    publish({ state: 'ready' }); publish({ state: 'ready' }); await client.serial;
    expect(pair).toHaveBeenCalledOnce();
    for (let i = 0; i < 3; i++) publish({ state: 'pairing_compare', comparison_code: '042917' });
    await client.serial;
    expect(request.mock.calls.filter(([path]) => path.endsWith('/pairing'))).toHaveLength(1);
    expect(received.at(-1).state).toBe(lost ? 'pairing_unconfirmed' : 'pairing_checking');
    expect(received.some(next => 'comparison_code' in next)).toBe(false);
    client.stop(); publish({ state: 'pairing_compare', comparison_code: '042917' }); await client.serial;
    expect(pair).toHaveBeenCalledOnce();
  });
  it('renders pairing status without showing either comparison code or declaring completion', () => {
    document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
    const value = payload(); value.task.purpose = 'browser.pairing-confirmation';
    for (const state of ['pairing_connecting', 'pairing_checking', 'pairing_unconfirmed', 'pairing_access_required']) {
      renderBrowserTask(document, { state, payload: value });
      expect(document.querySelector('main').dataset.journeyState).toBe('browser.' + state);
      expect(document.getElementById('task-title').textContent).toBe('Connect browser');
      expect(document.getElementById('authentication-open').hidden).toBe(true);
      expect(document.getElementById('extension-open').hidden).toBe(true);
      expect(document.getElementById('desktop-return').hidden).toBe(state === 'pairing_connecting');
    }
  });
});


it('replaces a saved pairing only after explicit action and current admission', async () => {
  const value = payload(); value.task.purpose = 'browser.pairing-confirmation'; value.creator_account_id = 'synthetic-creator';
  let publish;
  const replacePairing = vi.fn(() => true), receive = vi.fn();
  const request = vi.fn(async path => ({ ok: true, text: async () => JSON.stringify(path.endsWith('/session')
    ? { role: 'browser_task', generation: value.task.admission_generation, task_id: value.task.task_id,
      journey_id: value.task.journey_id, csrf_token: 'A'.repeat(43) } : value) }));
  const client = new BrowserTaskClient({ generation: value.task.admission_generation, request, receive,
    connect: ({ receive }) => { publish = receive; return { close: vi.fn(), replacePairing }; } });
  await client.start();
  publish({ state: 'pairing_saved' });
  expect(replacePairing).not.toHaveBeenCalled();
  const first = client.replacePairing();
  expect(await client.replacePairing()).toBe(false);
  expect(await first).toBe(true);
  expect(replacePairing).toHaveBeenCalledOnce();
  expect(request.mock.calls.filter(([path]) => path.endsWith('/payload'))).toHaveLength(2);
  client.stop();
});

it.each(['pairing_saved', 'auth_unconfirmed'])('restores the last %s presentation after a transient status-read failure', async state => {
  vi.useFakeTimers();
  try {
    const value = payload();
    value.task.purpose = state === 'pairing_saved' ? 'browser.pairing-confirmation' : 'browser.access';
    if (state === 'pairing_saved') value.creator_account_id = 'synthetic-creator';
    let publish, unavailable = false;
    const receive = vi.fn(), pair = vi.fn(), authenticate = vi.fn();
    const request = vi.fn(async path => {
      if (unavailable) return { ok: false, status: 503 };
      return { ok: true, text: async () => JSON.stringify(path.endsWith('/session')
        ? { role: 'browser_task', generation: value.task.admission_generation, task_id: value.task.task_id,
          journey_id: value.task.journey_id, csrf_token: 'A'.repeat(43) } : value) };
    });
    const client = new BrowserTaskClient({ generation: value.task.admission_generation, request, receive,
      connect: ({ receive }) => { publish = receive; return { close: vi.fn(), pair, authenticate }; } });
    await client.start(); publish({ state });
    unavailable = true;
    await vi.advanceTimersByTimeAsync(2000);
    expect(receive.mock.calls.at(-1)[0].state).toBe('service_unavailable');
    unavailable = false;
    await vi.advanceTimersByTimeAsync(4000);
    expect(receive.mock.calls.at(-1)[0].state).toBe(state);
    expect(request.mock.calls.every(([, options]) => options.method === undefined)).toBe(true);
    expect(pair).not.toHaveBeenCalled(); expect(authenticate).not.toHaveBeenCalled();
    client.stop();
  } finally { vi.useRealTimers(); }
});
