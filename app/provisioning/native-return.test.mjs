import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { createNativeReturn } from './native-return.js';
import { parseBrainOnboardingState } from './provisioning.js';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';

const journey = '11111111-1111-4111-8111-111111111111';
const snapshot = JSON.parse(readFileSync(new URL('../../shared/onboarding/vectors.json', import.meta.url)))
  .cases.find((item) => item.id === 'brain-snapshot').value;
const response = (status = 200, value = snapshot) => new Response(JSON.stringify(value), { status,
  headers: { 'Content-Type': 'application/json', 'X-Onboarding-Capabilities': 'local-onboarding.v1' } });
function fixture({ replies = [response()], result = { ok: true, result: { status: 'returned' } }, nativeEntry = false, ...overrides } = {}) {
  const calls = []; const messages = []; const status = {}; const retry = {}; const navigations = []; let closes = 0;
  const location = { origin: 'http://bridge.localhost:17871', pathname: '/provisioning/native-return',
    hash: `#journey=${journey}`, search: '', replace: (target) => navigations.push(target) };
  const options = { fetch: async (path, request) => {
    if (!nativeEntry && location.hash && path === '/api/v1/provisioning/native-entry') return response(401);
    calls.push({ path, request }); return replies.shift(); },
    location, status, retry, extensionId: 'a'.repeat(32), close: () => { closes++; },
    history: { replaceState(_state, _unused, target) { location.hash = new URL(target, location.origin).hash; } },
    loadEntryParser: async () => ({ parseOnboardingJson }),
    runtime: { sendMessage: async (...args) => { messages.push(args);
      return args[1].type === 'ofca.workspace.recovery-prepare.v1' && result?.result?.status === 'returned'
        ? { ok: true, result: { status: 'launch_pending', journey_id: journey } } : result; } },
    loadValidation: async () => [{ parseBrainOnboardingState }, { parseOnboardingJson }], ...overrides };
  const run = createNativeReturn(options);
  return { run, options, calls, messages, status, retry, navigations, get closes() { return closes; } };
}
for (const runtime of [false, true]) for (const authRequired of [false, true]) {
  test(`native return requests only registered navigation runtime=${runtime} authRequired=${authRequired}`, async () => {
    const f = fixture({ replies: [...(runtime ? [response(404, {})] : []), response(authRequired ? 401 : 200)] });
    assert.equal(await f.run(), true);
    assert.deepEqual(f.messages, [['a'.repeat(32), { type: 'ofca.workspace.launch-return.v1', journey_id: journey,
      route: runtime ? 'bridge' : 'provisioning' }]]);
    for (const { request } of f.calls) {
      assert.equal(request.credentials, 'same-origin'); assert.equal(request.cache, 'no-store');
      assert.equal(request.redirect, 'error'); assert.deepEqual(request.headers,
        { Accept: 'application/json', 'X-Onboarding-Journey': journey });
    }
    assert.equal(f.closes, 1); assert.deepEqual(f.navigations, []);
    assert.equal(await f.run(), false); assert.equal(f.messages.length, 1);
  });
}
test('unknown route or authority state, malformed snapshot and network failure never send a navigation request', async () => {
  for (const reply of [response(403, {}), response(409, {}), response(503, {}), response(200, {}),
    response(200, { ...snapshot, journey_id: '22222222-2222-4222-8222-222222222222' }),
    new Response('<h1>not found</h1>', { headers: { 'Content-Type': 'text/html' } })]) {
    const f = fixture({ replies: [reply] });
    assert.equal(await f.run(), false); assert.equal(f.messages.length, 0); assert.equal(f.closes, 0);
    assert.deepEqual(f.navigations, []); assert.equal(f.retry.hidden, false);
  }
  const f = fixture({ fetch: async () => { throw new Error('fixture unavailable'); } });
  assert.equal(await f.run(), false); assert.equal(f.messages.length, 0);
});
test('wrong caller URL, query or journey never reads authority', async () => {
  for (const delta of [{ origin: 'https://example.test' }, { pathname: '/provisioning' },
    { search: '?code=secret' }, { hash: '#journey=invalid' }, { hash: `#journey=${journey}&x=1` }]) {
    const f = fixture(); Object.assign(f.options.location, delta);
    assert.equal(await f.run(), false); assert.equal(f.calls.length, 0);
  }
});
test('no extension or explicit no workspace continues locally; existing or ambiguous owner never does', async () => {
  for (const options of [{ runtime: undefined }, { result: { ok: false, code: 'no_workspace' } }]) {
    const f = fixture(options); assert.equal(await f.run(), true);
    assert.deepEqual(f.navigations, [`/provisioning#journey=${journey}`]); assert.equal(f.closes, 0);
  }
  for (const result of [{ ok: false, code: 'workspace_exists' }, { ok: false, code: 'return_unavailable' },
    { ok: true, result: { status: 'returned', cookie: 'unexpected' } }, undefined]) {
    const f = fixture({ runtime: { sendMessage: async () => result } });
    assert.equal(await f.run(), false); assert.deepEqual(f.navigations, []); assert.equal(f.closes, 0);
  }
});
test('duplicate click is single-flight and a lost reply requires explicit receipt reconciliation', async () => {
  let resolve; let sends = 0;
  const f = fixture({ replies: [response(), response()], timeoutMs: 15,
    runtime: { sendMessage: () => { sends++; return sends === 1 ? new Promise((done) => { resolve = done; })
      : Promise.resolve({ ok: true, result: { status: 'returned' } }); } } });
  const first = f.run(); assert.equal(await f.run(), false); assert.equal(await first, false);
  assert.equal(sends, 1); assert.equal(f.closes, 0); assert.deepEqual(f.navigations, []);
  assert.equal(f.status.textContent, 'Setup could not be opened.');
  resolve({ ok: true, result: { status: 'returned' } }); await Promise.resolve();
  assert.equal(f.closes, 0);
  assert.equal(await f.run(), true); assert.equal(sends, 2); assert.equal(f.closes, 1);
});
test('pagehide fences a delayed reply and a restored document reads current state', async () => {
  let resolve;
  const f = fixture({ replies: [response(), response(409, {})],
    runtime: { sendMessage: () => new Promise((done) => { resolve = done; }) } });
  const pending = f.run(); while (!resolve) await new Promise((done) => setImmediate(done));
  f.run.stop(); resolve({ ok: true, result: { status: 'returned' } });
  assert.equal(await pending, false); assert.equal(f.closes, 0);
  assert.equal(await f.run(), false); assert.equal(f.calls.length, 2); assert.deepEqual(f.navigations, []);
});
test('browser refusal to close leaves factual guidance only after confirmed owner navigation', async () => {
  const f = fixture({ close: () => { throw new Error('browser refused'); } });
  assert.equal(await f.run(), true); assert.equal(f.status.textContent, 'Continue in your setup tab. You can close this tab.');
});

const entryId = '33333333-3333-4333-8333-333333333333';
const entryContext = { state: 'select_workspace', csrf_token: 'c'.repeat(43), entry_id: entryId };
const selected = { state: 'selected', journey_id: journey };
const memory = () => { const values = new Map(); return { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) }; };
const pendingLaunch = { ok: true, result: { status: 'launch_pending', journey_id: journey } };

test('targeted native entry verifies its bound launch before selection without proof disclosure', async () => {
  const f = fixture({ nativeEntry: true, storage: memory(),
    replies: [response(200, entryContext), response(200, selected), response()] });
  assert.equal(await f.run(), true);
  assert.deepEqual(f.calls.map(({ path, request }) => [path, request.method ?? 'GET']), [
    ['/api/v1/provisioning/native-entry', 'GET'], ['/api/v1/provisioning/native-entry', 'POST'],
    ['/api/v1/provisioning/state', 'GET'],
  ]);
  assert.deepEqual(JSON.parse(f.calls[1].request.body), { journey_id: journey });
  assert.equal(f.calls[1].request.headers['X-Provisioning-CSRF'], entryContext.csrf_token);
  assert.deepEqual(f.messages, [['a'.repeat(32), { type: 'ofca.workspace.recovery-prepare.v1', entry_id: entryId }],
    ['a'.repeat(32), { type: 'ofca.workspace.launch-return.v1',
    journey_id: journey, route: 'provisioning' }]]);
  assert.equal(f.closes, 1);
});

for (const configured of [false, true]) test(`manual native entry discovers before selecting local journey configured=${configured}`, async () => {
  const sent = []; const f = fixture({ storage: memory(),
    replies: configured ? [response(404), response(404), response(401)] : [response(200, entryContext), response(200, selected), response()],
    runtime: { sendMessage: async (_id, message) => { sent.push(message); return sent.length === 1
      ? { ok: true, result: { status: configured ? 'pending_launch' : 'launch_pending', journey_id: journey } }
      : { ok: true, result: { status: 'returned' } }; } } });
  f.options.location.hash = '';
  assert.equal(await f.run(), true);
  assert.deepEqual(sent, [configured ? { type: 'ofca.workspace.launch-discover.v1' }
    : { type: 'ofca.workspace.recovery-prepare.v1', entry_id: entryId },
    { type: 'ofca.workspace.launch-return.v1', journey_id: journey, route: configured ? 'bridge' : 'provisioning' }]);
  const posts = f.calls.filter(({ request }) => request.method === 'POST');
  assert.equal(posts.length, configured ? 0 : 1);
  if (!configured) {
    assert.deepEqual(JSON.parse(posts[0].request.body), { journey_id: journey });
    assert.equal(posts[0].request.headers['X-Provisioning-CSRF'], entryContext.csrf_token);
  }
  assert.equal(JSON.stringify(sent).includes(entryContext.csrf_token), false);
  assert.equal(f.options.location.hash, ''); assert.equal(f.closes, 1);
});

test('manual discovery with an existing or uncertain owner never selects a competing local journey', async () => {
  for (const code of ['workspace_exists', 'return_unavailable']) {
    const focus = { hidden: true };
    const f = fixture({ storage: memory(), focus, replies: [response(200, entryContext)], result: { ok: false, code } });
    f.options.location.hash = '';
    assert.equal(await f.run(), false); assert.equal(f.calls.length, 1); assert.deepEqual(f.navigations, []);
    assert.equal(focus.hidden, true);
  }
});

test('lost native selection reply only reads receipt; a fresh document never replays uncertain selection', async () => {
  const storage = memory(); const calls = [];
  const fetch = async (path, request) => { calls.push({ path, request });
    if (request.method === 'POST') throw new Error('reply lost');
    return response(200, entryContext);
  };
  const options = { fetch, storage, result: pendingLaunch };
  const f = fixture(options); f.options.location.hash = '';
  assert.equal(await f.run(), false);
  assert.equal(calls.filter(({ request }) => request.method === 'POST').length, 1);
  assert.equal(calls.length, 3);
  const restored = fixture(options); restored.options.location.hash = '';
  assert.equal(await restored.run(), false);
  assert.equal(calls.filter(({ request }) => request.method === 'POST').length, 1);
  assert.equal(restored.messages.length, 0);
});

test('a committed native selection with cookie receipt resumes without selection replay', async () => {
  const f = fixture({ storage: memory(), replies: [response(200, selected), response()] });
  f.options.location.hash = '';
  assert.equal(await f.run(), true); assert.equal(f.calls.length, 2);
  assert.ok(f.calls.every(({ request }) => request.method !== 'POST'));
  assert.equal(f.messages.length, 1); assert.equal(f.messages[0][1].type, 'ofca.workspace.launch-return.v1');
});

test('missing native proof and duplicate context properties never discover or select', async () => {
  for (const reply of [response(401, {}), new Response('{"state":"unconfirmed","state":"select_workspace","csrf_token":"'
    + 'c'.repeat(43) + '","entry_id":"' + entryId + '"}', { headers: { 'Content-Type': 'application/json' } })]) {
    const f = fixture({ replies: [reply], storage: memory() }); f.options.location.hash = '';
    assert.equal(await f.run(), false); assert.equal(f.messages.length, 0); assert.equal(f.calls.length, 1);
  }
});

const renewedJourney = '44444444-4444-4444-8444-444444444444';
const recoveryId = '55555555-5555-4555-8555-555555555555';
const recoveryReady = { ok: true, result: { status: 'recovery_ready', recovery_id: recoveryId, previous_journey_id: journey } };
const recovered = { state: 'selected', journey_id: renewedJourney, previous_journey_id: journey };
function recoveryFixture({ returnStatus = 'returned', ...options } = {}) {
  const messages = [];
  const f = fixture({ nativeEntry: true, storage: memory(),
    replies: [response(200, entryContext), response(200, recovered), response(200, { ...snapshot, journey_id: renewedJourney })],
    runtime: { sendMessage: async (_id, message) => { messages.push(message); return message.type === 'ofca.workspace.recovery-prepare.v1'
      ? recoveryReady : { ok: true, result: { status: returnStatus } }; } }, ...options });
  return { ...f, recoveryMessages: messages, closed: () => f.closes };
}

for (const returnStatus of ['returned', 'continued']) test(`renewal sends the exact recovery union and ${returnStatus} preserves owner`, async () => {
  const f = recoveryFixture({ returnStatus });
  assert.equal(await f.run(), true);
  assert.deepEqual(JSON.parse(f.calls[1].request.body), { journey_id: journey, recover: true });
  assert.deepEqual(f.recoveryMessages, [{ type: 'ofca.workspace.recovery-prepare.v1', entry_id: entryId },
    { type: 'ofca.workspace.recovery-return.v1', entry_id: entryId, recovery_id: recoveryId,
      previous_journey_id: journey, journey_id: renewedJourney, route: 'provisioning' }]);
  assert.equal(f.closed(), returnStatus === 'returned' ? 1 : 0); assert.deepEqual(f.navigations, []);
  assert.equal(JSON.stringify(f.recoveryMessages).includes(entryContext.csrf_token), false);
});

test('lost recovery selection and navigation replies use authenticated reads without another POST', async () => {
  let posts = 0, entries = 0, returns = 0;
  const storage = memory(); const messages = [];
  const f = recoveryFixture({ storage,
    fetch: async (path, request) => {
      if (path.endsWith('native-entry')) {
        if (request.method === 'POST') { posts++; throw Error('lost response'); }
        return response(200, entries++ === 0 ? entryContext : recovered);
      }
      return response(200, { ...snapshot, journey_id: renewedJourney });
    }, runtime: { sendMessage: async (_id, message) => { messages.push(message);
      if (message.type === 'ofca.workspace.recovery-prepare.v1') return recoveryReady;
      if (++returns === 1) throw Error('lost receipt');
      return { ok: true, result: { status: 'returned' } };
    } } });
  assert.equal(await f.run(), false); assert.equal(posts, 1);
  assert.equal(await f.run(), true); assert.equal(posts, 1);
  assert.equal(messages.filter((m) => m.type === 'ofca.workspace.recovery-prepare.v1').length, 1);
  assert.equal(returns, 2); assert.equal(f.closed(), 1);
});

test('unknown recovery selection stays read-only after retry or document recreation', async () => {
  let posts = 0; const storage = memory();
  const options = { storage, fetch: async (_path, request) => {
    if (request.method === 'POST') { posts++; throw Error('lost'); }
    return response(200, posts ? { state: 'unconfirmed' } : entryContext);
  } };
  const f = recoveryFixture(options);
  assert.equal(await f.run(), false); assert.equal(await f.run(), false);
  assert.equal(await recoveryFixture(options).run(), false);
  assert.equal(posts, 1); assert.equal(f.recoveryMessages.length, 1); assert.deepEqual(f.navigations, []);
});

for (const value of [{ state: 'selected', journey_id: renewedJourney }, { ...recovered, previous_journey_id: renewedJourney },
  { ...recovered, extra: true }, { state: 'unconfirmed' }]) {
  test(`recovery refuses unbound selected result ${JSON.stringify(value)}`, async () => {
    const f = recoveryFixture({ replies: [response(200, entryContext), response(200, value)] });
    assert.equal(await f.run(), false); assert.equal(f.recoveryMessages.length, 1);
    assert.deepEqual(f.navigations, []); assert.equal(f.closed(), 0);
  });
}

test('consumed recovery GET without its stored exact intent never calls the extension or navigates', async () => {
  for (const storage of [memory(), undefined]) {
    const f = recoveryFixture({ storage, replies: [response(200, recovered)] });
    assert.equal(await f.run(), false); assert.equal(f.recoveryMessages.length, 0); assert.deepEqual(f.navigations, []);
  }
});

for (const reply of [response(401, {}), response(403, {}), response(409, {}), response(200, { state: 'unconfirmed' }),
  response(200, { ...entryContext, extra: true })]) {
  test(`invalid bare native context ${reply.status} never prepares recovery`, async () => {
    const f = recoveryFixture({ replies: [reply] }); f.options.location.hash = '';
    assert.equal(await f.run(), false); assert.equal(f.recoveryMessages.length, 0); assert.equal(f.calls.length, 1);
  });
}

test('recovery never falls through to runtime routes or generic workspace fallback', async () => {
  for (const code of ['no_workspace', 'workspace_exists', 'return_unavailable']) {
    const f = recoveryFixture({ runtime: { sendMessage: async (_id, message) => message.type.endsWith('prepare.v1')
      ? recoveryReady : { ok: false, code } } });
    assert.equal(await f.run(), false); assert.deepEqual(f.navigations, []); assert.equal(f.closed(), 0);
  }
  const f = recoveryFixture({ replies: [response(200, entryContext), response(200, recovered), response(404)] });
  assert.equal(await f.run(), false); assert.equal(f.calls.length, 3); assert.equal(f.recoveryMessages.length, 1);
});

test('expired first launch uses the confirmed extension owner without selection, reload or extra confirmation', async () => {
  const f = recoveryFixture({ replies: [response(200, entryContext)],
    runtime: { sendMessage: async () => ({ ok: true, result: { status: 'launch_expired' } }) } });
  assert.equal(await f.run(), true); assert.equal(f.calls.length, 1); assert.equal(f.closed(), 1);
  assert.deepEqual(f.navigations, []);
});

test('pagehide after recovery preparation prevents selection and navigation', async () => {
  let release; const f = recoveryFixture({ replies: [response(200, entryContext)],
    runtime: { sendMessage: () => new Promise((resolve) => { release = resolve; }) } });
  const pending = f.run(); while (!release) await new Promise((resolve) => setImmediate(resolve));
  f.run.stop(); release(recoveryReady);
  assert.equal(await pending, false); assert.equal(f.calls.length, 1); assert.equal(f.closed(), 0);
});

test('a definitive recovery refusal stays factual and a retry cannot replay selection', async () => {
  const f = recoveryFixture({ replies: [response(200, entryContext), response(409, { detail: 'Onboarding scope is unavailable' }),
    response(200, entryContext), response(200, entryContext)] });
  assert.equal(await f.run(), false); assert.equal(f.status.textContent, 'Setup could not be recovered.');
  assert.equal(f.retry.hidden, true, 'a confirmed refusal has no repetitive retry action');
  assert.equal(await f.run(), false);
  assert.equal(f.calls.filter(({ request }) => request.method === 'POST').length, 1);
  assert.equal(f.closed(), 0); assert.deepEqual(f.navigations, []);
});

for (const noOwner of [false, true]) test(`targeted native context can recover without extension ownership, noWorkspace=${noOwner}`, async () => {
  const contexts = []; let closes = 0;
  const f = fixture({ nativeEntry: true, storage: memory(),
    replies: [response(200, { ...entryContext, target_journey_id: journey }), response(200, recovered),
      response(200, { ...snapshot, journey_id: renewedJourney })],
    runtime: noOwner ? { sendMessage: async () => ({ ok: false, code: 'no_workspace' }) } : undefined,
    close: () => { closes++; }, loadLocalWorkspace: async () => ({ returnToLocalWorkspace: async ({ context }) => {
      contexts.push(context); return { status: noOwner ? 'returned' : 'continued' };
    } }) });
  assert.equal(await f.run(), true);
  assert.deepEqual(JSON.parse(f.calls[1].request.body), { journey_id: journey, recover: true });
  assert.deepEqual(contexts, [{ entry_id: entryId, previous_journey_id: journey, journey_id: renewedJourney }]);
  assert.equal(closes, noOwner ? 1 : 0);
  assert.deepEqual(f.navigations, noOwner ? [] : [`/provisioning#journey=${renewedJourney}`]);
});

test('bound target mismatch and ambiguous extension failure cannot enter local recovery', async () => {
  for (const options of [
    { replies: [response(200, { ...entryContext, target_journey_id: renewedJourney })], runtime: undefined },
    { replies: [response(200, { ...entryContext, target_journey_id: journey })], runtime: { sendMessage: async () => { throw Error('unknown owner'); } } },
  ]) {
    const f = fixture({ nativeEntry: true, storage: memory(), ...options });
    assert.equal(await f.run(), false); assert.equal(f.calls.length, 1); assert.deepEqual(f.navigations, []);
  }
});

test('a matching selected read wins over a recovery POST refusal', async () => {
  const f = recoveryFixture({ replies: [response(200, entryContext), response(409, { detail: 'Native entry was used' }),
    response(200, recovered), response(200, { ...snapshot, journey_id: renewedJourney })] });
  assert.equal(await f.run(), true); assert.equal(f.closed(), 1);
});
