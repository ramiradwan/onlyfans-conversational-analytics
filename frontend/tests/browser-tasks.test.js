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

const browserTaskHtml = readFileSync('browser-tasks/index.html', 'utf8');
const pairingPayload = () => {
  const value = payload();
  value.task.purpose = 'browser.pairing-confirmation';
  value.creator_account_id = 'synthetic-creator';
  return value;
};
const taskControls = ['pairing-replace', 'extension-open', 'authentication-open', 'extension-install', 'desktop-return'];
const desktop = ['desktop-return'];
const browserTaskCases = [
  ['loading', 'Loading browser task', '', [], () => null],
  ['unavailable', 'Set up the extension', 'Install or turn on the extension in this browser profile.', ['extension-install', ...desktop]],
  ['extension_disconnected', 'Continue in the desktop app', 'Dipsy couldn’t connect to the extension.', desktop],
  ['profile_mismatch', 'Switch browser profile', 'This isn’t the profile you used for setup.', desktop],
  ['account_mismatch', 'Switch OnlyFans account', 'Sign in to the OnlyFans account you want to connect.', desktop, pairingPayload],
  ['needs-consent', 'Review the extension', 'See what it collects before you turn it on.', ['extension-open']],
  ['needs-permission', 'Allow site access', 'The extension needs it to read activity from your creator account.', ['extension-open']],
  ['needs-account', 'Sign in to your creator account', 'Use your creator account in this browser profile.', ['extension-open']],
  ['auth_idle', 'Finish signing in', '', ['authentication-open'], authenticationPayload],
  ['auth_opening', 'Signing in…', 'Finish in the sign-in window.', [], authenticationPayload],
  ['auth_checking', 'Sign in', 'Checking the sign-in outcome…', desktop, authenticationPayload],
  ['auth_unconfirmed', 'Sign in', 'Checking whether sign-in finished…', desktop, authenticationPayload],
  ['auth_access_required', 'Sign in', 'Browser access is required for this request.', desktop, authenticationPayload],
  ['auth_closed', 'Sign in', 'Sign-in window closed. Continue in the desktop app.', desktop, authenticationPayload],
  ['pairing_saved', 'Replace the saved connection?', 'This browser profile is already connected to a desktop app.', ['pairing-replace'], pairingPayload],
  ['pairing_connecting', 'Connect browser', 'Connecting this browser to the desktop app…', [], pairingPayload],
  ['pairing_checking', 'Connect browser', 'Checking the browser connection…', desktop, pairingPayload],
  ['pairing_unconfirmed', 'Connect browser', 'The browser connection has not been confirmed.', desktop, pairingPayload],
  ['pairing_access_required', 'Connect browser', 'Browser access is required to connect.', desktop, pairingPayload],
  ['ready', 'Confirming browser access…', '', []],
  ['paused', 'Message collection is paused', 'Letting the desktop app know…', []],
  ['reported', 'Browser access confirmed', 'This tab closes by itself.', desktop],
  ['status_reconnecting', 'Waiting for the desktop app', 'This page updates by itself.', []],
  ['task_completed', 'Continue in the desktop app', 'This step is done.', desktop],
  ['task_expired', 'Continue in the desktop app', 'This step expired.', desktop],
  ['task_changed', 'Continue in the desktop app', 'Setup moved on.', desktop],
  ['service_unavailable', 'Browser access', 'Dipsy can’t confirm this step. Continue setup in the desktop app.', desktop],
  ['service_unavailable', 'Continue in the desktop app', 'This step couldn’t finish.', desktop, payload, 'invariant_failed'],
  ['service_unavailable', 'Continue in the desktop app', 'This page can’t continue setup.', desktop, payload, 'authorization_refused'],
];

describe('Browser task presentation', () => {
  it('shows inert support only for a classified refusal with an empty action slot', () => {
    document.documentElement.innerHTML = browserTaskHtml;
    document.getElementById('support-channels').innerHTML = '<span data-channel="tiktok">https://help.example.com/a&amp;copy;b</span>';
    const refusal = { state: 'service_unavailable', recovery: 'invariant_failed', payload: null };
    const render = value => {
      renderBrowserTask(document, value);
      return document.querySelector('.task-actions-slot [data-support-help]:not([hidden])');
    };
    const help = render(refusal);
    expect(help.parentElement).toBe(document.querySelector('.task-actions-slot > [data-region-content]'));
    expect(help.parentElement.nextElementSibling).toBe(document.getElementById('task-status'));
    expect(help.querySelector('h2').textContent).toBe('Get help');
    expect([...help.querySelectorAll('li')].map(item => item.textContent)).toEqual(['TikTok https://help.example.com/a&copy;b']);
    expect(help.querySelector('a, button, input')).toBeNull();
    for (const value of [{ ...refusal, payload: payload() }, { ...refusal, recovery: undefined },
      { ...refusal, state: 'status_reconnecting' }, { ...refusal, state: 'loading' }]) expect(render(value)).toBeNull();
    document.getElementById('support-channels').remove();
    expect(render(refusal)).toBeNull();
  });

  it.each(browserTaskCases)('uses the per-state D4 title and copy for %s (%s)', (state, title, message, controls, makePayload = payload, recovery) => {
    document.documentElement.innerHTML = browserTaskHtml;
    const value = makePayload();
    renderBrowserTask(document, { state, payload: value, recovery });
    expect(document.querySelector('main').dataset.journeyState).toBe('browser.' + state.replaceAll('-', '_'));
    expect(document.querySelector('h1').textContent).toBe(title);
    expect(document.querySelector('#task-status').textContent).toBe(message);
    expect(taskControls.filter(id => !document.getElementById(id).hidden)).toEqual(controls);
    if (controls.includes('desktop-return')) {
      expect(document.getElementById('desktop-return').textContent).toBe('Open desktop app');
      expect(document.getElementById('desktop-return').getAttribute('href')).toBe('ofca://onboarding/?journey=' + value.task.journey_id);
    }
    if (state === 'needs-account') expect(document.getElementById('extension-open').textContent).toBe('Open creator account');
  });

  it('paints an accessible placeholder before the task owner responds', () => {
    document.documentElement.innerHTML = browserTaskHtml;
    expect(document.querySelector('.setup-task-card').getAttribute('aria-busy')).toBe('true');
    expect(document.querySelector('h1').textContent).toBe('Loading browser task');
    expect(document.querySelector('#task-status').textContent).toBe('');
    expect(taskControls.every(id => document.getElementById(id).hidden)).toBe(true);
    renderBrowserTask(document, { state: 'needs-permission', payload: payload() });
    expect(document.querySelector('.setup-task-card').getAttribute('aria-busy')).toBeNull();
    expect(document.querySelector('.setup-eyebrow').textContent).toBe('Browser setup');
  });

  it('keeps account and profile mismatch separate without treating observed identity as a binding', () => {
    document.documentElement.innerHTML = browserTaskHtml;
    const value = { ...pairingPayload(), creator_account_id: 'chrome-id-sentinel',
      creator_label: { display_name: 'Chrome name sentinel', username: 'chrome-handle-sentinel', initials: 'ZZ' } };
    for (const [state, message] of [
      ['account_mismatch', 'Sign in to the OnlyFans account you want to connect.'],
      ['profile_mismatch', 'This isn’t the profile you used for setup.'],
    ]) {
      renderBrowserTask(document, { state, payload: value });
      expect(document.querySelector('#task-status').textContent).toBe(message);
      expect(document.querySelector('main').outerHTML).not.toMatch(/chrome-(id|handle)-sentinel|Chrome name sentinel|\bZZ\b/);
      expect(taskControls.filter(id => !document.getElementById(id).hidden)).toEqual(desktop);
    }
  });

  it.each(['unavailable', 'needs-permission', 'auth_idle', 'pairing_saved', 'service_unavailable'])(
    'clears scoped links and actions when %s has no payload', state => {
      document.documentElement.innerHTML = browserTaskHtml;
      renderBrowserTask(document, { state: 'unavailable', payload: payload() });
      renderBrowserTask(document, { state });
      expect(taskControls.every(id => document.getElementById(id).hidden)).toBe(true);
      for (const id of ['extension-install', 'desktop-return']) expect(document.getElementById(id).hasAttribute('href')).toBe(false);
    });

  it('retains one guarded replacement control through card swaps', () => {
    document.documentElement.innerHTML = browserTaskHtml;
    const replace = document.getElementById('pairing-replace');
    const onReplace = vi.fn();
    replace.addEventListener('click', onReplace);
    renderBrowserTask(document, { state: 'pairing_saved', payload: authenticationPayload() });
    expect(replace.hidden).toBe(true);
    const before = document.querySelector('.setup-task-card');
    renderBrowserTask(document, { state: 'pairing_saved', payload: pairingPayload() });
    expect(document.querySelector('.setup-task-card')).not.toBe(before);
    expect(document.querySelectorAll('#pairing-replace:not([hidden])')).toHaveLength(1);
    expect(document.getElementById('pairing-replace')).toBe(replace);
    replace.click();
    renderBrowserTask(document, { state: 'pairing_connecting', payload: pairingPayload() });
    expect(replace.hidden).toBe(true);
    expect(onReplace).toHaveBeenCalledOnce();
  });

  it('distinguishes access reporting from the pairing owner’s completion', () => {
    document.documentElement.innerHTML = browserTaskHtml;
    renderBrowserTask(document, { state: 'reported', payload: payload() });
    expect(document.querySelector('h1').textContent).toBe('Browser access confirmed');
    renderBrowserTask(document, { state: 'task_completed', payload: pairingPayload() });
    expect(document.querySelector('h1').textContent).toBe('Continue in the desktop app');
    expect(document.querySelector('#task-status').textContent).toBe('This step is done.');
  });
});

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
  it.each(['tasks', 'open'])('fences a delayed %s reply after the native launch owner stops', async stage => {
    const value = authenticationPayload(), invoke = vi.fn();
    let release;
    const json = vi.fn(async path => {
      if (stage === 'tasks' && path.endsWith('/browser-tasks') || path.endsWith('/' + stage)) await new Promise(resolve => { release = resolve; });
      return path.endsWith('/open') ? { state: 'entry', entry_code: 'A'.repeat(43) } : { tasks: [value.task] };
    });
    const client = new DesktopSetupClient({ generation: id(4), receive: vi.fn(), storage: {},
      openBrowser: (owner, operation, _invoke, current) => openBrowserTask(owner, operation, invoke, current) });
    client.json = json; client.connected = true; client.session = { stage: 'setup' };
    client.launchOperation = value.task.operation_id;
    const pending = client.launchBrowser();
    await Promise.resolve(); await Promise.resolve();
    client.stop(); release(); await pending;
    expect(invoke).not.toHaveBeenCalled();
    expect(json.mock.calls.some(([path]) => path.endsWith('/open'))).toBe(stage === 'open');
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
    for (const state of ['loading', 'unavailable', 'profile_mismatch', 'account_mismatch', 'task_expired', 'task_changed', 'service_unavailable', 'status_reconnecting',
      'needs-consent', 'needs-permission', 'needs-account', 'ready', 'paused', 'reported', 'task_completed']) {
      renderBrowserTask(document, { state, payload: payload() });
      expect(document.querySelector('main').dataset.journeyState).toBe('browser.' + state.replaceAll('-', '_'));
      expect(document.getElementById('task-status').textContent.length > 10).toBe(!['loading', 'ready'].includes(state));
      expect(document.querySelector('nav')).toBeNull();
      if (['task_expired', 'task_changed', 'profile_mismatch', 'task_completed'].includes(state)) {
        expect(document.getElementById('desktop-return').hidden).toBe(false);
        expect(document.getElementById('desktop-return').getAttribute('href')).toBe('ofca://onboarding/?journey=' + payload().task.journey_id);
        expect(document.getElementById('extension-open').hidden).toBe(true);
      }
    }
  });
  it('returns blocked browser setup to its desktop owner without restarting the helper', () => {
    document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
    for (const state of ['unavailable', 'extension_disconnected', 'account_mismatch', 'profile_mismatch', 'service_unavailable']) {
      const value = payload();
      renderBrowserTask(document, { state, payload: value });
      expect(document.getElementById('task-retry')).toBeNull();
      expect(document.getElementById('desktop-return').hidden).toBe(false);
      expect(document.getElementById('desktop-return').getAttribute('href')).toBe('ofca://onboarding/?journey=' + value.task.journey_id);
      expect(document.getElementById('authentication-open').hidden).toBe(true);
      expect(document.getElementById('extension-open').hidden).toBe(true);
      expect(document.getElementById('extension-install').hidden).toBe(state !== 'unavailable');
    }
    renderBrowserTask(document, { state: 'service_unavailable' });
    expect(document.getElementById('desktop-return').hidden).toBe(true);
    expect(document.getElementById('task-status').textContent).toBe('Dipsy can’t connect right now. Open the desktop app to continue setup.');
  });
  it.each(['session', 'payload'])('recovers an initial %s read before attaching to the browser', async stage => {
    vi.useFakeTimers();
    const value = authenticationPayload(), receive = vi.fn(), authenticate = vi.fn(() => true);
    let unavailable = true, publish;
    const request = vi.fn(async path => {
      if (unavailable && path.endsWith('/' + stage)) return { ok: false, status: 503 };
      return { ok: true, text: async () => JSON.stringify(path.endsWith('/session')
        ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) } : value) };
    });
    const connect = vi.fn(({ receive: send }) => { publish = send; return { close: vi.fn(), authenticate }; });
    const events = vi.fn(() => new Promise(() => {}));
    const client = new BrowserTaskClient({ generation: id(4), request, receive, connect, events, nativeLaunch: true });
    try {
      const started = client.start();
      await vi.advanceTimersByTimeAsync(0);
      expect(receive.mock.calls.at(-1)[0].state).toBe('status_reconnecting');
      expect(connect).not.toHaveBeenCalled();
      unavailable = false;
      await vi.advanceTimersByTimeAsync(1000); await started;
      expect(connect).toHaveBeenCalledOnce();
      expect(events).toHaveBeenCalledOnce();
      expect(authenticate).not.toHaveBeenCalled();
      publish({ state: 'auth_idle' }); await client.serial;
      expect(authenticate).toHaveBeenCalledOnce();
      expect(request.mock.calls.filter(([path]) => path.endsWith('/' + stage)).length).toBe(stage === 'session' ? 2 : 3);
      expect(request.mock.calls.filter(([path]) => !path.endsWith('/timing')).every(([, options]) => options.method === undefined)).toBe(true);
    } finally { client.stop(); vi.useRealTimers(); }
  });
  it('bounds initial connection recovery and never attaches after its budget is exhausted', async () => {
    vi.useFakeTimers();
    const request = vi.fn(async () => ({ ok: false, status: 503 }));
    const connect = vi.fn(), receive = vi.fn(), events = vi.fn();
    const client = new BrowserTaskClient({ generation: id(4), request, connect, receive, events });
    try {
      const started = client.start();
      await vi.advanceTimersByTimeAsync(7000); await started;
      expect(request.mock.calls.filter(([path]) => path.endsWith('/session'))).toHaveLength(4);
      const payloadCalls = request.mock.calls.filter(([path]) => path.endsWith('/payload')).length;
      expect(payloadCalls).toBeGreaterThanOrEqual(1);
      expect(payloadCalls).toBeLessThanOrEqual(4);
      expect(receive.mock.calls.at(-1)[0].state).toBe('service_unavailable');
      await vi.advanceTimersByTimeAsync(60000);
      expect(request.mock.calls.filter(([path]) => path.endsWith('/session'))).toHaveLength(4);
      expect(request.mock.calls.filter(([path]) => path.endsWith('/payload'))).toHaveLength(payloadCalls);
      expect(connect).not.toHaveBeenCalled(); expect(events).not.toHaveBeenCalled();
    } finally { client.stop(); vi.useRealTimers(); }
  });
  it.each([401, 403, 409])('does not retry a refused initial session: %s', async status => {
    vi.useFakeTimers();
    const request = vi.fn(async () => ({ ok: false, status }));
    const connect = vi.fn(), receive = vi.fn();
    const client = new BrowserTaskClient({ generation: id(4), request, connect, receive });
    try {
      await client.start();
      await vi.advanceTimersByTimeAsync(60000);
      expect(request.mock.calls.filter(([path]) => path.endsWith('/session'))).toHaveLength(1);
      expect(request.mock.calls.filter(([path]) => path.endsWith('/payload'))).toHaveLength(1);
      expect(connect).not.toHaveBeenCalled();
      expect(receive.mock.calls.at(-1)[0].state).toBe(status === 403 ? 'service_unavailable' : 'task_changed');
    } finally { client.stop(); vi.useRealTimers(); }
  });
  it('does not retry an invalid initial response or attach to the extension', async () => {
    vi.useFakeTimers();
    const request = vi.fn(async () => ({ ok: true, text: async () => '{"role":"browser_task","role":"browser_task"}' }));
    const connect = vi.fn(), receive = vi.fn();
    const client = new BrowserTaskClient({ generation: id(4), request, connect, receive });
    try {
      await client.start();
      await vi.advanceTimersByTimeAsync(60000);
      expect(request.mock.calls.filter(([path]) => path.endsWith('/session'))).toHaveLength(1);
      expect(request.mock.calls.filter(([path]) => path.endsWith('/payload'))).toHaveLength(1);
      expect(connect).not.toHaveBeenCalled();
      expect(receive.mock.calls.at(-1)[0].state).toBe('service_unavailable');
    } finally { client.stop(); vi.useRealTimers(); }
  });
  it.each(['timeout', 'delay', 'in-flight'])('retires initial connection recovery when the page closes: %s', async phase => {
    vi.useFakeTimers();
    const receive = vi.fn(), connect = vi.fn();
    const request = vi.fn(async () => phase === 'delay' ? { ok: false, status: 503 } : new Promise(() => {}));
    const client = new BrowserTaskClient({ generation: id(4), request, receive, connect });
    try {
      const started = client.start();
      await vi.advanceTimersByTimeAsync(phase === 'timeout' ? 3000 : 0);
      const calls = request.mock.calls.length;
      const signal = request.mock.calls[0][1].signal;
      client.stop(); await started;
      await vi.advanceTimersByTimeAsync(60000);
      if (phase !== 'delay') expect(signal.aborted).toBe(true);
      expect(request).toHaveBeenCalledTimes(calls);
      expect(connect).not.toHaveBeenCalled();
      expect(receive.mock.calls.at(-1)[0].state).not.toBe('service_unavailable');
    } finally { client.stop(); vi.useRealTimers(); }
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
    expect(receive.mock.calls.at(-1)[0].state).toBe('invalid_response');
    expect(link.open()).toBe(false); expect(port.disconnect).toHaveBeenCalledOnce();
  });
  it('does not deliver intermediate consent decisions to Brain and reports only the admitted account observation', async () => {
    const value = payload(), received = [], requests = [];
    let publish;
    const request = vi.fn(async (path, options) => {
      if (!path.endsWith('/events')) requests.push([path, options]);
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
  it('opens one prepared access review with its fresh native entry and public generation', async () => {
    const value = payload(), invoke = vi.fn();
    const client = { json: vi.fn(async path => path.endsWith('/open') ? { state: 'entry', entry_code: 'A'.repeat(43), presentation: id(9) } : { tasks: [value.task] }) };
    expect(await openBrowserTask(client, value.task.operation_id, invoke)).toBe(true);
    expect(invoke).toHaveBeenCalledExactlyOnceWith('A'.repeat(43), id(9));
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
      expect(request.mock.calls.filter(([path]) => !path.endsWith('/events'))).toHaveLength(3);
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
      expect(document.getElementById('authentication-browser')).toBeNull();
      expect(connect).toHaveBeenCalledTimes(source === 'start' ? 0 : 1);
      expect(authenticate).not.toHaveBeenCalled();
      expect(request.mock.calls.filter(([path]) => !path.endsWith('/timing'))
        .every(([, options]) => options.method === undefined)).toBe(true);
    } finally { client.stop(); vi.useRealTimers(); }
  });
  it.each(['authentication', 'access'])('reads the end of an open %s task at its own interval', async kind => {
    vi.useFakeTimers();
    const value = kind === 'authentication' ? authenticationPayload() : payload(), received = [];
    const finish = vi.fn(async () => true), close = vi.fn();
    let completed = false, unavailable = false;
    const reads = () => request.mock.calls.filter(([path]) => path.endsWith('/payload')).length;
    const request = vi.fn(async path => {
      if (unavailable && path.endsWith('/payload')) return { ok: false, status: 503 };
      return { ok: true, text: async () => JSON.stringify(path.endsWith('/session')
        ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) }
        : completed ? { ...value, task: { ...value.task, state: 'completed' }, ceremony: kind === 'authentication' ? null : value.ceremony }
          : value) };
    });
    const client = new BrowserTaskClient({ generation: id(4), request, receive: next => received.push(next),
      connect: () => ({ finish, close }) });
    try {
      await client.start();
      const started = reads();
      await vi.advanceTimersByTimeAsync(500);
      expect(reads() - started).toBe(kind === 'authentication' ? 1 : 0);
      // A failed status read backs off without requiring customer intervention.
      unavailable = true;
      await vi.advanceTimersByTimeAsync(kind === 'authentication' ? 500 : 1500);
      expect(received.at(-1).state).toBe('status_reconnecting');
      document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
      renderBrowserTask(document, received.at(-1));
      expect(document.getElementById('task-status').textContent).toBe('This page updates by itself.');
      expect([...document.querySelectorAll('.setup-actions > *')].every(element => element.hidden)).toBe(true);
      expect(close).not.toHaveBeenCalled();
      unavailable = false;
      await vi.advanceTimersByTimeAsync(kind === 'authentication' ? 1000 : 4000);
      completed = true;
      const before = reads();
      await vi.advanceTimersByTimeAsync(kind === 'authentication' ? 500 : 2000);
      expect(reads() - before).toBe(1);
      expect(received.at(-1).state).toBe('task_completed');
      expect(finish).toHaveBeenCalledOnce();
      await vi.advanceTimersByTimeAsync(5000);
      expect(reads() - before).toBe(1);
    } finally { client.stop(); vi.useRealTimers(); }
  });
  it('renders automatic status recovery without a retry action', () => {
    document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
    renderBrowserTask(document, { state: 'status_reconnecting', payload: authenticationPayload() });
    expect(document.getElementById('task-status').textContent).toBe('This page updates by itself.');
    expect([...document.querySelectorAll('.setup-actions > *')].every(element => element.hidden)).toBe(true);
    expect(document.querySelector('main').dataset.journeyState).toBe('browser.status_reconnecting');
  });
  it.each(['restore', 'changed', 'expired'])('reconciles interrupted status reads without restarting sign-in: %s', async outcome => {
    vi.useFakeTimers();
    const value = authenticationPayload(), receive = vi.fn(), authenticate = vi.fn(), close = vi.fn();
    let publish, unavailable = false, changed = false;
    const request = vi.fn(async path => {
      if (path.endsWith('/payload') && unavailable) return { ok: false, status: 503 };
      if (path.endsWith('/payload') && changed) return { ok: false, status: 409 };
      return { ok: true, text: async () => JSON.stringify(path.endsWith('/session')
        ? { role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) }
        : value) };
    });
    const connect = vi.fn(({ receive: send }) => { publish = send; return { close, authenticate }; });
    const events = vi.fn(() => new Promise(() => {}));
    const client = new BrowserTaskClient({ generation: id(4), request, connect, receive, events });
    try {
      await client.start();
      publish({ state: 'auth_idle' });
      unavailable = true;
      await vi.advanceTimersByTimeAsync(500);
      expect(receive.mock.calls.at(-1)[0].state).toBe('status_reconnecting');
      await vi.advanceTimersByTimeAsync(1000);
      expect(receive.mock.calls.at(-1)[0].state).toBe('status_reconnecting');
      unavailable = false;
      changed = outcome === 'changed';
      if (outcome === 'expired') client.now = () => Date.parse(value.ceremony.expires_at) + 1;
      await vi.advanceTimersByTimeAsync(2000);
      expect(receive.mock.calls.at(-1)[0].state).toBe(outcome === 'restore' ? 'auth_idle'
        : outcome === 'changed' ? 'task_changed' : 'task_expired');
      expect(connect).toHaveBeenCalledOnce();
      expect(authenticate).not.toHaveBeenCalled();
      expect(request.mock.calls.filter(([path]) => path.endsWith('/session'))).toHaveLength(1);
      expect(request.mock.calls.every(([path, options]) => options.method === undefined || path.endsWith('/ended'))).toBe(true);
      expect(close).toHaveBeenCalledTimes(outcome === 'restore' ? 0 : 1);
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
  it.each([['setup', 'dispatched'], ['runtime', 'dispatched'], ['setup', 'focused'], ['setup', 'entry'], ['runtime', 'unavailable'], ['setup', 'malformed']])(
    'lets Brain choose the route in the %s stage and opens Chrome only for an entry: %s', async (stage, state) => {
      const value = authenticationPayload(), invoke = vi.fn();
      const result = state === 'entry' ? { state, entry_code: 'A'.repeat(43) } : state === 'malformed' ? { state: 'entry' } : { state };
      const client = { session: { stage }, json: vi.fn(async path => path.endsWith('/open') ? result : { tasks: [value.task] }) };
      if (state === 'malformed') await expect(openBrowserTask(client, value.task.operation_id, invoke)).rejects.toThrow('browser_unavailable');
      else expect(await openBrowserTask(client, value.task.operation_id, invoke)).toBe(state !== 'unavailable');
      expect(client.json.mock.calls.map(([path]) => path.split('/').at(-1))).toEqual(['browser-tasks', 'open']);
      expect(invoke).toHaveBeenCalledTimes(state === 'entry' ? 1 : 0);
      if (state === 'entry') expect(invoke).toHaveBeenCalledWith('A'.repeat(43));
    });
  it('renders focused authentication and ordinary Chrome recovery without declaring success', () => {
    document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
    const value = authenticationPayload();
    for (const state of ['auth_idle', 'auth_opening', 'auth_checking', 'auth_unconfirmed', 'auth_access_required', 'unavailable']) {
      renderBrowserTask(document, { state, payload: value });
      expect(document.querySelector('main').dataset.journeyState).toBe('browser.' + state);
      expect(document.getElementById('authentication-open').hidden).toBe(state !== 'auth_idle');
      expect(document.getElementById('extension-open').hidden).toBe(true);
    }
    // Sign-in stays in the extension popup. No second Chrome route exists.
    expect(document.getElementById('authentication-browser')).toBeNull();
    expect(document.querySelector('a[href*="#system-browser"]')).toBeNull();
    expect(document.getElementById('extension-install').target).toBe('_self');
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
      expect(receive.mock.calls.at(-1)[0].state).toBe(invalid ? 'invalid_response' : 'auth_checking');
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
    expect(await client.authenticate()).toBe(false);
    publish({ state: 'auth_idle', notification: null });
    expect(received.at(-1).state).toBe('auth_unconfirmed');
    expect(await client.authenticate()).toBe(false);
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
      expect(receive).toHaveBeenLastCalledWith({ state: 'connection_lost' });
      expect(link.open()).toBe(false);
      const count = port.postMessage.mock.calls.length;
      await vi.advanceTimersByTimeAsync(40000);
      expect(port.postMessage.mock.calls.length).toBe(count);
      link.close();
    } finally { vi.useRealTimers(); }
  });
  it('reconciles a dropped port refusal without reusing a refused admission on restart', async () => {
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
    // The only write is this helper's own ending report.
    expect(request.mock.calls.filter(([path]) => !path.endsWith('/ended')).every(([, options]) => options.method === undefined)).toBe(true);
    await client.start();
    expect(received.at(-1)).toEqual({ state: 'task_changed', payload: null });
    expect(client.session).toBeNull();
    expect(client.lastLink).toBeNull();
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
      expect(receive.mock.calls.at(-1)[0]).toEqual(changed ? { state: 'invalid_response' }
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
    expect(receive).toHaveBeenLastCalledWith({ state: kind === 'exact' ? 'pairing_completed' : 'invalid_response' });
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
        expect(request.mock.calls.filter(([path, options]) => options.method === 'POST' && !path.endsWith('/ended'))).toHaveLength(1);
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
    expect(receive.mock.calls.at(-1)[0].state).toBe('status_reconnecting');
    unavailable = false;
    await vi.advanceTimersByTimeAsync(4000);
    expect(receive.mock.calls.at(-1)[0].state).toBe(state);
    expect(request.mock.calls.every(([, options]) => options.method === undefined)).toBe(true);
    expect(pair).not.toHaveBeenCalled(); expect(authenticate).not.toHaveBeenCalled();
    client.stop();
  } finally { vi.useRealTimers(); }
});

describe('sign-in helper opened before its ceremony', () => {
  const preparing = ready => { const value = structuredClone(ready); value.task.state = 'pending'; value.ceremony = null; return value; };
  const run = async (replies, { lost = false } = {}) => {
    const ready = authenticationPayload(); ready.task.state = 'pending';
    const reads = replies(ready), receive = vi.fn(), links = [];
    const connect = vi.fn(() => { const link = { finish: vi.fn(), close: vi.fn(), attach: vi.fn(() => !lost) }; links.push(link); return link; });
    const request = vi.fn(async path => ({ ok: true, text: async () => JSON.stringify(path.endsWith('/session')
      ? { role: 'browser_task', generation: ready.task.admission_generation, task_id: ready.task.task_id,
        journey_id: ready.task.journey_id, csrf_token: 'A'.repeat(43) } : reads.shift() ?? reads.last) }));
    const client = new BrowserTaskClient({ generation: ready.task.admission_generation, request, receive, connect });
    return { ready, receive, connect, links, request, client };
  };
  it('prepares on one port and attaches that port to the ceremony after reading the preparing task again', async () => {
    vi.useFakeTimers();
    const h = await run(ready => Object.assign([preparing(ready), preparing(ready), ready], { last: ready }));
    try {
      const started = h.client.start();
      await vi.advanceTimersByTimeAsync(600); await started;
      expect(h.connect).toHaveBeenCalledOnce();
      expect(h.connect.mock.calls[0][0].prepare).toBe(true);
      expect(h.connect.mock.calls[0][0].payload.ceremony).toBeNull();
      expect(h.links[0].attach).toHaveBeenCalledOnce();
      expect(h.links[0].attach.mock.calls[0][0].ceremony).toEqual(h.ready.ceremony);
      expect(h.request.mock.calls.filter(([path]) => path.endsWith('/payload'))).toHaveLength(3);
    } finally { h.client.stop(); vi.useRealTimers(); }
  });
  it('attaches within a tenth of a second of the ceremony arriving', async () => {
    vi.useFakeTimers();
    const h = await run(ready => Object.assign([preparing(ready), ready], { last: ready }));
    try {
      const started = h.client.start();
      await vi.advanceTimersByTimeAsync(100); await started;
      expect(h.links[0].attach).toHaveBeenCalledOnce();
    } finally { h.client.stop(); vi.useRealTimers(); }
  });
  it('attaches on a fresh port when the preparing port was lost', async () => {
    vi.useFakeTimers();
    const h = await run(ready => Object.assign([preparing(ready), ready], { last: ready }), { lost: true });
    try {
      const started = h.client.start();
      await vi.advanceTimersByTimeAsync(100); await started;
      expect(h.connect).toHaveBeenCalledTimes(2);
      expect(h.links[0].close).toHaveBeenCalled();
      expect(h.connect.mock.calls[1][0].prepare).toBeUndefined();
      expect(h.connect.mock.calls[1][0].payload.ceremony).toEqual(h.ready.ceremony);
    } finally { h.client.stop(); vi.useRealTimers(); }
  });
  it.each(['cancelled', 'changed'])('attaches nothing and closes the preparing port when the preparing task is %s', async change => {
    vi.useFakeTimers();
    const h = await run(ready => {
      const next = preparing(ready);
      if (change === 'cancelled') next.task.state = 'cancelled'; else next.task.expires_at += 60;
      return Object.assign([preparing(ready), next], { last: next });
    });
    try {
      const started = h.client.start();
      await vi.advanceTimersByTimeAsync(600); await started;
      expect(h.connect).toHaveBeenCalledOnce();
      expect(h.connect.mock.calls[0][0].prepare).toBe(true);
      expect(h.links[0].attach).not.toHaveBeenCalled();
      expect(h.links[0].close).toHaveBeenCalled();
      expect(h.receive.mock.calls.at(-1)[0].state).toBe('task_changed');
    } finally { h.client.stop(); vi.useRealTimers(); }
  });
});

function reconnectingHelper(value, options = {}) {
  const ports = [], received = [], posts = [];
  let outcome = value;
  const request = vi.fn(async (path, requestOptions) => {
    if (requestOptions.method === 'POST') posts.push([path.split('/').at(-1), JSON.parse(requestOptions.body)]);
    const response = options.respond?.(path, requestOptions);
    if (response) return response;
    const body = path.endsWith('/session') ? { role: 'browser_task', generation: id(4), task_id: id(1),
      journey_id: id(2), csrf_token: 'A'.repeat(43) } : path.endsWith('/payload') ? outcome : { state: 'checking' };
    return { ok: true, text: async () => JSON.stringify(body) };
  });
  const connect = vi.fn(({ receive }) => {
    const port = { receive, close: vi.fn(), authenticate: vi.fn(() => true), pair: vi.fn(() => true),
      open: vi.fn(() => true) };
    ports.push(port);
    return port;
  });
  const client = new BrowserTaskClient({ generation: id(4), nativeLaunch: options.nativeLaunch ?? false,
    receive: state => received.push(state), request, connect, reattach: vi.fn(async () => false),
    events: ({ signal }) => new Promise(resolve => signal.addEventListener('abort', resolve, { once: true })) });
  return { client, connect, received, ports, posts, setOutcome: next => { outcome = next; } };
}

it('reports extension disconnection after a verified port drops without reopening a second port or repeating sign-in', async () => {
  const value = authenticationPayload(), h = reconnectingHelper(value, { nativeLaunch: true });
  try {
    await h.client.start();
    h.ports[0].receive({ state: 'auth_idle', notification: null });
    await h.client.serial;
    expect(h.ports[0].authenticate).toHaveBeenCalledOnce();
    h.ports[0].receive({ state: 'auth_opening' });
    const notification = { ceremony_id: value.ceremony.ceremony_id, attempt_id: value.ceremony.attempt_id, notification: 'unavailable' };
    h.ports[0].receive({ state: 'auth_checking', notification });
    await h.client.serial;
    expect(h.received.at(-1).state).toBe('auth_unconfirmed');
    expect(await h.client.authenticate()).toBe(false);
    expect(h.ports[0].authenticate).toHaveBeenCalledOnce();
    h.ports[0].receive({ state: 'connection_lost' });
    await h.client.serial; await h.client.ending;
    expect(h.connect).toHaveBeenCalledOnce();
    expect(h.received.at(-1).state).toBe('extension_disconnected');
    expect(h.posts.filter(([path]) => !['timing', 'opened'].includes(path))).toEqual([
      ['notification', notification], ['ended', { reason: 'unavailable' }],
    ]);
    document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
    renderBrowserTask(document, h.received.at(-1));
    expect(document.getElementById('extension-install').hidden).toBe(true);
    expect(document.getElementById('authentication-open').hidden).toBe(true);
    expect(document.getElementById('desktop-return').hidden).toBe(false);
  } finally { h.client.stop(); }
});

it('reconciles a completed task when a verified port drops without reopening a second port', async () => {
  const value = authenticationPayload(), h = reconnectingHelper(value);
  try {
    await h.client.start();
    h.ports[0].receive({ state: 'auth_opening' });
    h.setOutcome({ ...value, task: { ...value.task, state: 'completed' }, ceremony: null });
    h.ports[0].receive({ state: 'connection_lost' });
    await h.client.serial;
    expect(h.received.at(-1).state).toBe('task_completed');
    expect(h.connect).toHaveBeenCalledOnce();
    expect(h.ports[0].authenticate).not.toHaveBeenCalled();
    expect(h.posts.filter(([path]) => !['timing', 'opened'].includes(path))).toEqual([]);
  } finally { h.client.stop(); }
});

it('does not restart pairing or reattach after an invalid response or explicit refusal', async () => {
  for (const reason of ['connection_lost', 'invalid_response', 'extension_refused']) {
    const value = payload(); value.task.purpose = 'browser.pairing-confirmation'; value.creator_account_id = 'synthetic-creator';
    const h = reconnectingHelper(value);
    try {
      await h.client.start();
      h.ports[0].receive({ state: 'ready' }); await h.client.serial;
      expect(h.ports[0].pair).toHaveBeenCalledOnce();
      h.ports[0].receive({ state: reason }); await h.client.serial; await h.client.ending;
      expect(h.connect).toHaveBeenCalledOnce();
      expect(h.ports[0].pair).toHaveBeenCalledOnce();
      expect(h.received.at(-1).state).toBe(reason === 'invalid_response' ? 'service_unavailable' : 'extension_disconnected');
    } finally { h.client.stop(); }
  }
});

it('coalesces sign-in clicks and fences a delayed launch against a lost port', async () => {
  const value = authenticationPayload();
  let release;
  const h = reconnectingHelper(value, { respond: path => {
    if (path.endsWith('/payload') && release === null) return new Promise(resolve => { release = () =>
      resolve({ ok: true, text: async () => JSON.stringify(value) }); });
  } });
  try {
    await h.client.start();
    h.ports[0].receive({ state: 'auth_idle' });
    release = null;
    const first = h.client.authenticate();
    expect(await h.client.authenticate()).toBe(false);
    h.ports[0].receive({ state: 'connection_lost' });
    release();
    expect(await first).toBe(false);
    await h.client.serial;
    expect(h.connect).toHaveBeenCalledOnce();
    expect(h.ports[0].authenticate).not.toHaveBeenCalled();
    expect(h.received.at(-1).state).toBe('extension_disconnected');
  } finally { h.client.stop(); }
});

it('fails closed for an unknown browser presentation rather than recommending installation', () => {
  document.documentElement.innerHTML = readFileSync('browser-tasks/index.html', 'utf8');
  renderBrowserTask(document, { state: 'unknown', payload: payload() });
  expect(document.querySelector('main').dataset.journeyState).toBe('browser.service_unavailable');
  expect(document.getElementById('extension-install').hidden).toBe(true);
  expect(document.getElementById('desktop-return').hidden).toBe(false);
});

it('does not reattach when a fresh admission read refuses access after connection loss', async () => {
  let refused = false;
  const h = reconnectingHelper(authenticationPayload(), { respond: path =>
    refused && path.endsWith('/payload') ? { ok: false, status: 403 } : null });
  try {
    await h.client.start();
    h.ports[0].receive({ state: 'auth_idle' });
    refused = true;
    h.ports[0].receive({ state: 'connection_lost' });
    await h.client.serial;
    expect(h.received.at(-1).state).toBe('service_unavailable');
    expect(h.connect).toHaveBeenCalledOnce();
    expect(h.posts).toEqual([]);
  } finally { h.client.stop(); }
});

it('distinguishes an extension refusal from initial discovery failure on the actual port adapter', () => {
  const value = payload(), receive = vi.fn();
  const port = { onMessage: event(), onDisconnect: event(), postMessage: vi.fn(), disconnect: vi.fn() };
  const link = connectExtension({ payload: value, receive, runtime: { connect: () => port } });
  port.onMessage.emit({ type: 'refused', reason: 'unavailable' });
  expect(receive).toHaveBeenLastCalledWith({ state: 'extension_refused' });
  expect(link.open()).toBe(false);
  link.end();
});

it('does not report disconnection after the helper closes during the fresh admission read', async () => {
  const value = authenticationPayload();
  let release;
  const h = reconnectingHelper(value, { respond: path => {
    if (path.endsWith('/payload') && release === null) return new Promise(resolve => { release = () =>
      resolve({ ok: true, text: async () => JSON.stringify(value) }); });
  } });
  await h.client.start();
  h.ports[0].receive({ state: 'auth_idle' });
  release = null;
  h.ports[0].receive({ state: 'connection_lost' });
  await Promise.resolve();
  h.client.stop();
  release();
  await h.client.serial;
  expect(h.connect).toHaveBeenCalledOnce();
  expect(h.posts).toEqual([]);
});
