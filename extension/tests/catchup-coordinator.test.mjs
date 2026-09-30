import assert from 'node:assert/strict';
import test from 'node:test';
import { DurableIngestOutbox } from '../transport/read-only-durable-outbox.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';
import { traversalConfiguration, TRAVERSAL_ACCOUNT } from './signer-traversal-scenario.mjs';
import { normalizeSignerMessage } from '../transport/read-only-signer-normalization.mjs';
import { HistoryAcquisitionCoordinator } from '../transport/read-only-history-coordinator.mjs';
import { ReadOnlyAgentWebSocketClient } from '../transport/read-only-agent-websocket.mjs';
import { parseAgentToBrainMessage } from '../protocol/read-only.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';
import { createReadOnlyIndexedDbIngestionStorage } from '../transport/read-only-indexeddb-ingestion-storage.mjs';
import { ConsentController } from '../runtime/consent-controller.mjs';

const START = Date.parse('2026-09-29T12:00:00Z');
const stamp = (offset = 0) => new Date(START + offset).toISOString();
const conversation = (id, head = undefined) => ({ id, platform_user_id: id, display_name: null,
  updated_at: stamp(), ...(head === undefined ? {} : { head_sent_at: head, head_message_id: `m${id}` }) });
const message = (id, chat = '101', offset = -60_000) => ({ id, chat_id: chat,
  sender_platform_user_id: chat, text: 'Synthetic', sent_at: stamp(offset), direction: 'inbound' });
const page = (operation, items, continuation = null) => ({ success: true, operation,
  data: { items, continuation, boundary: continuation === null ? operation === 'conversations' ? 'inventory_end' : 'history_start' : null } });
const observing = () => ({ observing: true, reason: 'ok', runnable: true,
  tabs: { armed: 1, frozen: 0, discarded: 0 }, page_socket_open: true,
  drops: { expired: 0, rejected: 0 } });

async function rig({ list = [conversation('101')], messages = { '101': [message('m101')] },
  kind = 'catch_up', budget = 50, cap = 1000, read, rpc, storage = new InMemoryIngestionStorage() } = {}) {
  const { CatchupCoordinator } = await import('../transport/catchup-coordinator.mjs');
  const outbox = new DurableIngestOutbox({ storage, creatorAccountId: TRAVERSAL_ACCOUNT });
  await outbox.initialize();
  const r = { storage, outbox, clock: START, calls: [], rpcs: [], state: observing(),
    config: traversalConfiguration({ pages_per_wake: budget, page_size: 100 }),
    session: { creator_account_id: TRAVERSAL_ACCOUNT, applied_config_revision: 'synthetic-config-v1',
      config_auth_ticket: 'synthetic', agent_installation_id: crypto.randomUUID() } };
  r.grant = { result: 'granted', check_id: crypto.randomUUID(), kind, gap_epoch: 1,
    uncertain_since: stamp(), granted_at: stamp(), blind: false, page_budget: 200,
    lease_expires_at: stamp(300_000), resume: false };
  r.create = () => new CatchupCoordinator({ outbox, clock: () => r.clock, dailyCap: cap,
    configuration: () => r.config, session: () => r.session, captureState: async () => r.state,
    workerInstanceId: crypto.randomUUID(), delay: async () => {},
    signer: { async read(request) {
      r.calls.push({ operation: request.operation, cursor: request.parameters.query.cursor,
        chat: request.parameters.conversationId });
      if (read) return read(request, r);
      return page(request.operation, request.operation === 'conversations' ? list : messages[request.parameters.conversationId]);
    } },
    rpc: async (operation, request) => {
      r.rpcs.push({ operation, ...structuredClone(request) });
      if (rpc) return rpc(operation, request, r);
      if (operation === 'capture.state.report') return { acknowledged_seq: request.report_seq };
      return { ...r.grant, resume: request.active_check_id !== null,
        lease_expires_at: new Date(r.clock + 300_000).toISOString() };
    },
  });
  r.coordinator = r.create();
  r.job = () => outbox.historyJob('catchup:active');
  r.evidence = async (type) => (await outbox.entries()).filter(e => e.change.evidence?.type === type).map(e => e.change.evidence);
  r.ids = async () => [...storage.stores.get('messages').keys()].sort();
  r.passive = (value) => outbox.enqueue(normalizeSignerMessage(value, {
    observedAt: stamp(), creatorPlatformId: '9001', conversationId: value.chat_id }));
  return r;
}

test('disabled history keeps catch-up dormant until configuration enables it', async () => {
  const r = await rig();
  r.config = traversalConfiguration({ enabled: false });
  await r.coordinator.wake('admission');
  await r.coordinator.reportCaptureState();
  await r.coordinator.requestCaptureStateReport();
  await r.coordinator.onIngestAcknowledged({ committed_source_seq: 0 });
  assert.equal(r.rpcs.length, 0);
  assert.equal(r.calls.length, 0);

  r.config = traversalConfiguration({ enabled: true });
  await r.coordinator.wake('observing');
  assert.ok(r.rpcs.some(value => value.operation === 'capture.state.report'));
  assert.ok(r.rpcs.some(value => value.operation === 'history.check.begin'));
});

test('ties, missing heads and new chats have exact budgets and frozen heads', async () => {
  const r = await rig({ list: [conversation('101', stamp(-900_000)), conversation('102', stamp(-900_000)),
    conversation('103'), conversation('104', stamp(-900_001))],
    messages: { '101': [message('m101')], '102': [message('m102', '102')], '103': [message('m103', '103')] } });
  await r.coordinator.wake('admission');
  assert.deepEqual(await r.ids(), ['m101', 'm102', 'm103']);
  assert.equal(r.calls.length, 4);
  const [end] = await r.evidence('check.completed');
  assert.deepEqual(end.counts, { list: 1, messages: 2, probes: 1 });
  assert.equal(end.pages_read, 4);
  assert.equal((await r.evidence('check.chat_reconciled'))[0].target_head.sent_at, stamp(-900_000));
});

test('internal gap below a stored newest message is read to boundary', async () => {
  const r = await rig({ read: async (q) => q.operation === 'conversations'
    ? page(q.operation, [conversation('101', stamp(-60_000))])
    : q.parameters.query.cursor === null ? page(q.operation, [message('top')], 'older')
      : page(q.operation, [message('gap', '101', -300_000), message('old', '101', -900_001)]) });
  await r.passive(message('top'));
  await r.coordinator.wake();
  assert.deepEqual(await r.ids(), ['gap', 'old', 'top']);
  assert.equal(r.calls.length, 3);
  assert.equal((await r.evidence('check.chat_reconciled'))[0].reached, 'boundary');
});

test('mover omitted during scan is reconciled', async () => {
  const r = await rig({ read: async (q, r) => {
    if (q.operation === 'conversations') {
      await r.passive(message('live', '102', 1));
      return page(q.operation, [conversation('101', stamp(-900_001))]);
    }
    return page(q.operation, [message('live', '102', 1), message('missed', '102')]);
  } });
  await r.coordinator.wake();
  assert.deepEqual(await r.ids(), ['live', 'missed']);
  assert.equal(r.calls.length, 2);
  assert.equal((await r.evidence('check.inventory_closed'))[0].movers, 1);
});

test('mover arriving at the completion transaction prevents premature closure', async () => {
  const r = await rig({ list: [conversation('101', stamp(-900_001))],
    messages: { '102': [message('live', '102', 1), message('missed', '102')] } });
  const commit = r.outbox.commitPage.bind(r.outbox);
  let injected = false;
  r.outbox.commitPage = async args => {
    if (!injected && args.jobPatch.phase === 'completed') {
      injected = true;
      await r.passive(message('live', '102', 1));
    }
    return commit(args);
  };
  await r.coordinator.wake();
  assert.equal((await r.evidence('check.completed')).length, 0);
  assert.equal(r.calls.length, 1);
  await r.coordinator.wake();
  assert.deepEqual(await r.ids(), ['live', 'missed']);
  assert.equal(r.calls.length, 2);
  assert.equal((await r.evidence('check.inventory_closed'))[0].movers, 1);
  assert.equal((await r.evidence('check.completed')).length, 1);
});

for (const scenario of ['repeated', 'non-adjacent', 'empty']) {
  test(`${scenario} cursor ends incomplete within the exact budget`, async () => {
    const r = await rig({ read: async (q, r) => page(q.operation,
      scenario === 'empty' ? [] : [conversation(String(100 + r.calls.length))],
      scenario === 'non-adjacent' && r.calls.length === 2 ? 'second' : 'first') });
    await r.coordinator.wake();
    assert.equal(r.calls.length, scenario === 'empty' ? 1 : scenario === 'repeated' ? 2 : 3);
    assert.equal((await r.evidence('check.abandoned'))[0].reason, 'cursor_invalid');
    assert.equal((await r.evidence('check.completed')).length, 0);
  });
}

test('endless mover arrivals end incomplete after bounded closing passes', async () => {
  const r = await rig({ read: async (q, r) => {
    const next = String(100 + r.calls.length);
    await r.passive(message(`live${next}`, next, 1));
    return page(q.operation, q.operation === 'conversations' ? [] : [message(`old${q.parameters.conversationId}`, q.parameters.conversationId)]);
  } });
  await r.coordinator.wake();
  assert.equal(r.calls.length, 4);
  assert.equal((await r.evidence('check.completed')).length, 0);
  assert.equal((await r.evidence('check.abandoned'))[0].reason, 'retry_exhausted');
});

test('daily cap suspends and resumes with the same grant next UTC day', async () => {
  const r = await rig({ cap: 1 });
  await r.coordinator.wake();
  assert.equal(r.calls.length, 1);
  assert.equal((await r.evidence('check.completed')).length, 0);
  r.coordinator = r.create();
  await r.coordinator.wake();
  assert.equal(r.calls.length, 1);
  r.clock += 86_400_000;
  await r.coordinator.wake();
  assert.equal(r.calls.length, 2);
  assert.equal((await r.evidence('check.completed')).length, 1);
  assert.ok(r.rpcs.some(v => v.trigger === 'renew' && v.active_check_id === r.grant.check_id));
});

test('catchup and canary wakes do not multiply the persisted allowance', async () => {
  const r = await rig({ budget: 1 });
  for (let i = 0; i < 6; i++) { await r.coordinator.wake(); r.coordinator = r.create(); }
  assert.equal(r.calls.length, 1);
  r.clock += 60_000;
  await r.coordinator.wake();
  assert.equal(r.calls.length, 2);
});

test('reported document drops are not recounted after a worker restart or freeze', async () => {
  const r = await rig({ list: [] });
  r.state = { ...r.state, drops: { expired: 2, rejected: 1 }, drop_tabs: [7],
    drop_sources: { 7: { document: 'fixture-page', expired: 2, rejected: 1 } } };
  await r.coordinator.wake();
  r.coordinator.stop();
  r.coordinator = r.create();
  r.clock += 60_000;
  r.state.drop_sources = {};
  r.state.drops = { expired: 0, rejected: 0 };
  await r.coordinator.wake();
  r.clock += 60_000;
  r.state.drop_sources = { 7: { document: 'fixture-page', expired: 3, rejected: 1 } };
  r.state.drops = { expired: 3, rejected: 1 };
  await r.coordinator.wake();
  const reports = r.rpcs.filter(value => value.operation === 'capture.state.report');
  assert.deepEqual(reports.map(value => value.drops_since_last), [
    { expired: 2, rejected: 1 }, { expired: 0, rejected: 0 }, { expired: 1, rejected: 0 },
  ]);
  assert.equal(r.calls.length, 1);
});

test('canary ignores recent heads and attributes all probe pages only to its check', async () => {
  const r = await rig({ kind: 'canary', list: [conversation('101', stamp(-119_999)),
    conversation('102', stamp(-120_000)), conversation('103')], messages: { '103': [message('old', '103', -180_000)] } });
  await r.passive(message('unrelated'));
  await r.coordinator.wake();
  const [end] = await r.evidence('check.completed');
  assert.deepEqual(end.heads.map(h => h.chat_id), ['102', '103']);
  assert.deepEqual(end.counts, { list: 1, messages: 0, probes: 1 });
  assert.equal(r.calls.length, 2);
  for (const frame of await r.outbox.entries()) {
    assert.equal(frame.check_id, frame.acquisition_origin === 'passive' ? undefined : r.grant.check_id);
  }
  const client = Object.create(ReadOnlyAgentWebSocketClient.prototype);
  const sent = [];
  Object.assign(client, { outbox: r.outbox, syncRequired: false, session: {}, socket: { readyState: 1 },
    flushPromise: null, identity: { agentInstallationId: crypto.randomUUID(), agentStreamId: crypto.randomUUID(), lastAcknowledgedSourceSeq: 0 },
    sentSourceSeqs: new Set(), connectionContext: () => ({ assertCurrent() {} }),
    sendBound(type, payload) {
      parseAgentToBrainMessage({ type, protocol_version: '2', message_id: crypto.randomUUID(), correlation_id: null,
        payload: { ...payload, creator_account_id: TRAVERSAL_ACCOUNT, connection_id: crypto.randomUUID(), fencing_token: 'fixture' } });
      sent.push(payload);
      return true;
    },
  });
  while (client.identity.lastAcknowledgedSourceSeq < r.outbox.identityState().last_source_seq) {
    await client.flushOutbox();
    client.identity.lastAcknowledgedSourceSeq = sent.at(-1).source_seq;
    client.sentSourceSeqs.clear();
  }
  for (const frame of sent) assert.equal(frame.check_id, frame.acquisition_origin === 'passive' ? undefined : r.grant.check_id);
});

for (const stage of ['before-commit', 'after-commit', 'before-ack', 'after-ack']) {
  test(`restart ${stage} renews and resumes atomic page state`, async () => {
    const r = await rig({ budget: 1 });
    if (stage === 'before-commit' || stage === 'after-commit') {
      const original = r.outbox.commitPage.bind(r.outbox);
      let failed = false;
      r.outbox.commitPage = async args => {
        if (!failed && args.changes.length) {
          failed = true;
          if (stage === 'before-commit') r.storage.failNextWriteTransactionAfter(2);
          const committed = await original(args);
          if (stage === 'after-commit') throw new Error('Synthetic interruption');
          return committed;
        }
        return original(args);
      };
    }
    await r.coordinator.wake();
    if (stage === 'after-ack') await r.outbox.acknowledge(r.outbox.identityState().last_source_seq);
    r.clock += 60_000;
    r.coordinator = r.create();
    await r.coordinator.wake('admission');
    r.clock += 60_000;
    await r.coordinator.wake();
    assert.equal((await r.job()).phase, 'completed');
    assert.deepEqual(await r.ids(), ['m101']);
    assert.equal(r.calls.length, stage === 'before-commit' ? 3 : 2);
    assert.ok(r.rpcs.some(v => v.trigger === 'renew' && v.active_check_id === r.grant.check_id));
  });
}

for (const reason of ['paused', 'consent_needed', 'account_mismatch', 'tab_frozen']) {
  test(`${reason} mid-read fences material and completion`, async () => {
    const r = await rig({ read: async (q, r) => {
      r.state = { ...r.state, observing: false, runnable: false, reason };
      return page(q.operation, [conversation('101')]);
    } });
    await r.coordinator.wake();
    assert.equal(r.calls.length, 1);
    assert.equal(r.storage.stores.get('chats').size, 0);
    assert.equal((await r.evidence('check.completed')).length, 0);
  });
}

test('invariant failure abandons after at most one retry and awaits a new grant', async () => {
  const r = await rig({ messages: { '101': [{ ...message('conflict'), text: 'Different synthetic material' }] } });
  await r.passive(message('conflict'));
  for (let i = 0; i < 8; i++) { await r.coordinator.wake(); r.clock += 60_000; }
  assert.equal((await r.evidence('check.abandoned')).length, 1);
  assert.equal((await r.evidence('check.abandoned'))[0].reason, 'retry_exhausted');
  assert.equal(r.calls.length, 3);
  assert.equal((await r.evidence('check.completed')).length, 0);
  assert.equal(r.storage.stores.get('messages').get('conflict').text, 'Synthetic');
  await r.outbox.acknowledge(r.outbox.identityState().last_source_seq);
  await r.coordinator.wake();
  assert.equal(r.calls.length, 3);
  r.grant.check_id = crypto.randomUUID();
  await r.coordinator.wake();
  assert.equal((await r.job()).check_id, r.grant.check_id);
  assert.equal(r.calls.length, 5);
});

for (const mutation of ['revoke', 'account-switch', 'epoch-switch']) {
  test(`${mutation} during a page keeps the old partition fenced`, async () => {
    const r = await rig({ read: async (q, r) => {
      if (mutation === 'revoke') r.config.history_acquisition.enabled = false;
      if (mutation === 'account-switch') r.session.creator_account_id = 'other-synthetic';
      if (mutation === 'epoch-switch') await r.outbox.invalidateAccountEpoch();
      return page(q.operation, [conversation('101')]);
    } });
    await r.coordinator.wake();
    assert.equal(r.calls.length, 1);
    assert.equal(r.storage.stores.get('chats').size, 0);
    assert.equal((await r.evidence('check.completed')).length, 0);
  });
}

test('report counters stay fixed until ACK then include intervening requests', async () => {
  let loseAck = true;
  const r = await rig({ rpc: async (op, req, r) => {
    if (op === 'history.check.begin') return r.grant;
    if (loseAck) { loseAck = false; throw new Error('Lost ACK'); }
    return { acknowledged_seq: req.report_seq };
  } });
  await r.coordinator.wake();
  r.clock += 60_000;
  await r.coordinator.wake();
  const reports = r.rpcs.filter(v => v.operation === 'capture.state.report');
  assert.deepEqual(reports[0], reports[1]);
  r.clock += 60_000;
  await r.coordinator.wake();
  const last = r.rpcs.filter(v => v.operation === 'capture.state.report').at(-1);
  assert.equal(last.requests_since_last.catchup_list, 1);
  assert.equal(last.requests_since_last.catchup_messages, 1);
});

test('stopping capture cancels history synchronously and retains its report while disconnected', async () => {
  const { coordinateAcquisition } = await import('../transport/catchup-coordinator.mjs');
  const r = await rig({ list: [] });
  const session = r.session;
  r.session = null;
  r.state = { ...r.state, observing: false, runnable: false, reason: 'paused' };
  let canceled = false;
  const history = coordinateAcquisition({ cancelCurrent() { canceled = true; } }, r.coordinator);
  const stopping = history.reportCaptureState();
  assert.equal(canceled, true);
  await stopping;
  const control = await r.outbox.updateAcquisitionState(() => {});
  assert.equal(control.pending_report.reason, 'paused');
  assert.equal(r.rpcs.length, 0);
  r.session = session;
  r.state = observing();
  await r.coordinator.wake();
  assert.deepEqual(r.rpcs.filter(value => value.operation === 'capture.state.report').map(value => value.reason), ['paused', 'ok']);
  assert.equal(r.calls.length, 1);
});

test('concurrent stop reports and wakes share one pending acknowledgement', async () => {
  let release;
  let entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  const response = new Promise(resolve => { release = resolve; });
  const r = await rig({ rpc: async (operation, request) => {
    entered();
    await response;
    return { acknowledged_seq: request.report_seq };
  } });
  const first = r.coordinator.reportCaptureState();
  await waiting;
  const second = r.coordinator.reportCaptureState();
  const wake = r.coordinator.wake();
  const result = Promise.allSettled([first, second, wake]);
  release();
  assert.deepEqual((await result).map(value => value.status), ['fulfilled', 'fulfilled', 'fulfilled']);
  assert.equal(r.rpcs.length, 1);
  assert.equal(r.calls.length, 0);
});

test('notification reports carry socket and drop changes without an acquisition run', async () => {
  const r = await rig({ list: [] });
  r.state = { ...observing(), page_socket_open: false,
    drops: { expired: 0, rejected: 0 }, drop_tabs: [7],
    drop_sources: { 7: { document: 'status-page', expired: 0, rejected: 0 } } };
  await r.coordinator.requestCaptureStateReport();
  r.state = { ...r.state, page_socket_open: true };
  await r.coordinator.requestCaptureStateReport();
  r.state = { ...r.state, drops: { expired: 1, rejected: 2 },
    drop_sources: { 7: { document: 'status-page', expired: 1, rejected: 2 } } };
  await r.coordinator.requestCaptureStateReport();

  const reports = r.rpcs.filter(value => value.operation === 'capture.state.report');
  assert.deepEqual(reports.map(value => value.page_socket_open), [false, true, true]);
  assert.deepEqual(reports.map(value => value.drops_since_last), [
    { expired: 0, rejected: 0 }, { expired: 0, rejected: 0 }, { expired: 1, rejected: 2 },
  ]);
  assert.equal(r.calls.length, 0);
});

test('trusted page socket open and close notifications report each current state', async () => {
  let time = START;
  let nextTimer = 0;
  const timers = new Map();
  const scheduler = {
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, { callback, at: time + delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    tick(ms) {
      const end = time + ms;
      while (true) {
        const due = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!due || due[1].at > end) break;
        time = due[1].at;
        timers.delete(due[0]);
        due[1].callback();
      }
      time = end;
    },
  };
  const listeners = [];
  const chromeApi = {
    runtime: { id: 'synthetic-extension-id', onMessage: { addListener: fn => listeners.push(fn) } },
    storage: { local: { async get() { return {}; }, async set() {} } },
    scripting: {}, permissions: { onAdded: { addListener() {} }, onRemoved: { addListener() {} } },
  };
  const r = await rig({ list: [] });
  const controller = new ConsentController({
    chromeApi,
    runtime: { async start() {}, history: r.coordinator },
    adapter: { async loadBrainBinding() {}, async clearBrainBinding() {} },
    provisioningIdentityBridge: { register() {} },
    previewMetrics: { async record() {}, async summary() { return {}; }, async clear() {}, async prune() {} },
    clearLocalData: async () => {},
    activeModeAuthorization: {
      async authorizeTransition() { return true; }, async authorizeResume() { return true; },
      async reconcileActiveMode() { return true; },
    },
    scheduler, now: () => new Date(time),
  });
  controller.register();
  const listener = listeners[0];
  const sender = { id: chromeApi.runtime.id, frameId: 0, url: 'https://onlyfans.com/chats' };
  const notify = () => listener({ type: 'ofca.capture.state.changed' }, sender, () => assert.fail('no response'));
  const waitForReports = async expected => {
    for (let count = 0; count < 50; count++) {
      if (r.rpcs.filter(value => value.operation === 'capture.state.report').length >= expected) return;
      await new Promise(resolve => setImmediate(resolve));
    }
    assert.fail(`report ${expected} was not sent`);
  };

  r.state = { ...observing(), page_socket_open: false };
  notify();
  await waitForReports(1);
  scheduler.tick(5_000);
  r.clock += 5_000;
  r.state = { ...r.state, page_socket_open: true };
  notify();
  await waitForReports(2);
  scheduler.tick(5_000);
  r.clock += 5_000;
  r.state = { ...r.state, page_socket_open: false };
  notify();
  await waitForReports(3);

  const reports = r.rpcs.filter(value => value.operation === 'capture.state.report');
  assert.deepEqual(reports.map(value => value.page_socket_open), [false, true, false]);
  assert.equal(r.calls.length, 0);
});

test('notification report waits for a catch-up run without aborting it', async () => {
  let release;
  let entered;
  let finished = false;
  const waiting = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const r = await rig({ list: [], read: async q => {
    if (q.operation === 'conversations') { entered(); await gate; }
    return page(q.operation, []);
  } });
  r.grant.blind = true;
  const { coordinateAcquisition } = await import('../transport/catchup-coordinator.mjs');
  let historyCanceled = false;
  const coordinated = coordinateAcquisition({ cancelCurrent() { historyCanceled = true; } }, r.coordinator);
  const run = r.coordinator.wake('admission').then(() => { finished = true; });
  await waiting;
  r.state = { ...observing(), page_socket_open: false, drops: { expired: 1, rejected: 0 },
    drop_tabs: [7], drop_sources: { 7: { document: 'status-page', expired: 1, rejected: 0 } } };
  const report = coordinated.requestCaptureStateReport();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(finished, false, 'notification must not abort the running catch-up');
  assert.equal(historyCanceled, false, 'notification must not cancel history');
  assert.ok(r.rpcs.filter(value => value.operation === 'capture.state.report')
    .every(value => value.page_socket_open === true), 'the changed snapshot waits for catch-up to finish');
  release();
  await Promise.all([run, report]);

  assert.equal((await r.job()).phase, 'completed');
  const sent = r.rpcs.find(value => value.operation === 'capture.state.report' && !value.page_socket_open);
  assert.equal(sent.page_socket_open, false);
  assert.deepEqual(sent.drops_since_last, { expired: 1, rejected: 0 });
});

test('notifications arriving during an unacknowledged report retain the latest state serially', async () => {
  let release;
  let entered;
  let active = 0;
  let maximumActive = 0;
  const waiting = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const r = await rig({ list: [], rpc: async (operation, request) => {
    if (operation !== 'capture.state.report') return { ...r.grant, resume: false,
      lease_expires_at: new Date(r.clock + 300_000).toISOString() };
    active++;
    maximumActive = Math.max(maximumActive, active);
    if (active === 1) { entered(); await gate; }
    active--;
    return { acknowledged_seq: request.report_seq };
  } });
  const first = r.coordinator.requestCaptureStateReport();
  await waiting;
  r.state = { ...r.state, page_socket_open: false };
  const second = r.coordinator.requestCaptureStateReport();
  release();
  await Promise.all([first, second]);
  const reports = r.rpcs.filter(value => value.operation === 'capture.state.report');
  assert.deepEqual(reports.map(value => value.page_socket_open), [true, false]);
  assert.equal(maximumActive, 1);
});

test('refusing Brain backoff survives 24 hours of worker restarts', async () => {
  let closes = 0;
  let reconnects = 0;
  const r = await rig({ rpc: async (operation, request, r) => {
    closes++;
    r.session = null;
    r.coordinator.cancelCurrent();
    throw Object.assign(new Error('Refused'), { code: closes % 2 ? 'session_request_refused' : 'channel_closed' });
  } });
  const binding = { ...r.session };
  let historyPages = 0;
  const history = new HistoryAcquisitionCoordinator({ outbox: r.outbox,
    configuration: () => r.config, session: () => r.session, clock: () => r.clock,
    signer: { async read({ operation }) {
      if (operation === 'identity') return { operation, success: true, data: { id: '9001' } };
      historyPages++;
      return page(operation, [conversation(String(historyPages + 1000))], `next${historyPages}`);
    } }, delay: async () => {},
  });
  r.config.history_acquisition.pages_per_wake = 1;
  for (let minute = 0; minute < 1440; minute++) {
    r.clock = START + minute * 60_000;
    if (minute % 10 === 0) { r.coordinator.stop(); r.coordinator = r.create(); }
    await r.coordinator.wake('admission');
    const reconnect = r.session === null;
    if (reconnect) { reconnects++; r.session = { ...binding, connection_id: crypto.randomUUID() }; }
    if (reconnect || minute % 10 === 0) {
      await r.passive(message(`delivered${minute}`));
      await history.wake();
    }
  }
  assert.equal(r.rpcs.length, 29);
  assert.equal(closes, 29);
  assert.equal(reconnects, 29);
  assert.equal((await r.ids()).length, 172);
  assert.equal(r.calls.length, 0);
  assert.equal(historyPages, 172);
  assert.ok((await r.outbox.entries()).filter(e => e.acquisition_origin === 'signer').every(e => !('check_id' in e)));
});

test('history receives one reserved page within five minutes of catchup pressure', async () => {
  const r = await rig({ budget: 2, read: async (q, r) => page(q.operation, [conversation(String(r.calls.length + 100))], `next${r.calls.length}`) });
  await r.coordinator.allowance.historyPending(true);
  await r.coordinator.wake();
  assert.equal(r.calls.length, 1);
  assert.equal(await r.coordinator.allowance.reserve('history_list', 2), true);
  r.clock += 60_000;
  await r.coordinator.wake();
  assert.equal(r.calls.length, 3);
  r.clock += 240_000;
  await r.coordinator.wake();
  assert.equal(r.calls.length, 4);
  assert.equal(await r.coordinator.allowance.reserve('history_messages', 2), true);
});

test('half lease renews even when the minute allowance is exhausted', async () => {
  const r = await rig({ budget: 1 });
  await r.coordinator.wake();
  r.clock += 150_000;
  await r.coordinator.wake();
  assert.ok(r.rpcs.some(q => q.trigger === 'renew' && q.active_check_id === r.grant.check_id));
});

test('reaching local cap reports usage and requests suspension immediately', async () => {
  let automatic = 0;
  const r = await rig({ cap: 1, rpc: async (op, req, r) => {
    if (op === 'capture.state.report') { automatic = req.automatic_pages_today; return { acknowledged_seq: req.report_seq }; }
    if (automatic === 1) return { result: 'deferred', reason: 'daily_cap', retry_after_seconds: 43_200 };
    return r.grant;
  } });
  await r.coordinator.wake();
  assert.equal(automatic, 1);
  assert.ok(r.rpcs.some(q => q.trigger === 'renew'));
  assert.equal((await r.job()).phase, 'messages');
});

test('encrypted catchup jobs retain the cursor and allowance through reconstruction', async () => {
  const database = new FakeIndexedDb();
  const storage = createReadOnlyIndexedDbIngestionStorage(database, {
    databaseName: 'catchup-fixture', encryptionKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=' });
  const r = await rig({ storage, budget: 1 });
  await r.coordinator.wake();
  assert.equal((await r.job()).phase, 'messages');
  r.coordinator.stop();
  r.coordinator = r.create();
  await r.coordinator.wake('admission');
  assert.equal(r.calls.length, 1);
  r.clock += 60_000;
  await r.coordinator.wake();
  assert.equal((await r.job()).phase, 'completed');
  assert.equal(r.calls.length, 2);
});

test('lost grant response reuses the durable begin request after restart', async () => {
  let lost = false;
  const r = await rig({ rpc: async (op, request, r) => {
    if (op === 'capture.state.report') return { acknowledged_seq: request.report_seq };
    if (!lost) { lost = true; throw new Error('Lost grant response'); }
    return r.grant;
  } });
  await r.coordinator.wake('admission');
  r.coordinator.stop();
  r.coordinator = r.create();
  r.clock += 60_000;
  await r.coordinator.wake('admission');
  const begins = r.rpcs.filter(q => q.operation === 'history.check.begin');
  assert.equal(begins.length, 2);
  assert.equal(begins[1].request_id, begins[0].request_id);
  assert.equal((await r.job()).phase, 'completed');
});


test('a capture-state trigger arriving during a check is processed after that check', async () => {
  let release;
  let entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const r = await rig({ list: [], read: async q => {
    if (q.operation === 'conversations') {
      entered();
      await gate;
    }
    return page(q.operation, []);
  } });
  r.grant.blind = true;
  r.state = { ...observing(), observing: false, reason: 'page_socket_closed' };

  const first = r.coordinator.wake('admission');
  await waiting;
  r.state = observing();
  const second = r.coordinator.wake('observing');
  release();
  await Promise.all([first, second]);

  const reports = r.rpcs.filter(value => value.operation === 'capture.state.report');
  assert.ok(reports.some(value => value.observing === true));
  assert.equal(r.rpcs.filter(value => value.operation === 'history.check.begin'
    && value.trigger === 'observing').length, 0);

  await r.outbox.acknowledge(r.outbox.identityState().last_source_seq);
  await r.coordinator.onIngestAcknowledged();

  assert.ok(r.rpcs.some(value => value.operation === 'history.check.begin'
    && value.trigger === 'observing'));
});

test('coordinated acquisition preserves a trigger that arrives while acquisition is running', async () => {
  const { coordinateAcquisition } = await import('../transport/catchup-coordinator.mjs');
  let release;
  let entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const triggers = [];
  let historyWakes = 0;
  const catchup = {
    async wake(trigger) {
      triggers.push(trigger);
      if (triggers.length === 1) {
        entered();
        await gate;
      }
    },
    reportCaptureState: async () => {},
    cancelCurrent() {},
    stop() {},
  };
  const history = {
    async wake() { historyWakes++; },
    cancelCurrent() {},
    stop() {},
    historyErrorCode() { return null; },
  };
  const coordinated = coordinateAcquisition(history, catchup);

  const first = coordinated.wake('admission');
  await waiting;
  const second = coordinated.wake('observing');
  release();
  await Promise.all([first, second]);

  assert.deepEqual(triggers, ['admission', 'observing']);
  assert.equal(historyWakes, 2);
});
