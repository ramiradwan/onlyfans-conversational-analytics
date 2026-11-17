import assert from 'node:assert/strict';
import test from 'node:test';
import { HistoryAcquisitionCoordinator } from '../transport/history-coordinator.mjs';
import { DurableIngestOutbox } from '../transport/durable-outbox.mjs';
import { AgentWebSocketClient } from '../transport/agent-websocket.mjs';
import { HistoryAcquisitionCoordinator as ReadOnlyHistoryAcquisitionCoordinator } from '../transport/read-only-history-coordinator.mjs';
import { DurableIngestOutbox as ReadOnlyDurableIngestOutbox } from '../transport/read-only-durable-outbox.mjs';
import { ReadOnlyAgentWebSocketClient } from '../transport/read-only-agent-websocket.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';
import { traversalConfiguration, TRAVERSAL_ACCOUNT, TRAVERSAL_CREATOR, TRAVERSAL_TIME } from './signer-traversal-scenario.mjs';

const twins = [
  { name: 'generic', Outbox: DurableIngestOutbox, Transport: AgentWebSocketClient,
    Coordinator: HistoryAcquisitionCoordinator },
  { name: 'read-only', Outbox: ReadOnlyDurableIngestOutbox, Transport: ReadOnlyAgentWebSocketClient,
    Coordinator: ReadOnlyHistoryAcquisitionCoordinator },
];

async function rig({ Outbox, Transport, Coordinator }, { immediate = false, rejectFirstDrain = false, snapshotFence = false, enabled = true } = {}) {
  const outbox = new Outbox({ storage: new InMemoryIngestionStorage(), creatorAccountId: TRAVERSAL_ACCOUNT });
  const state = await outbox.initialize();
  const sent = [];
  const errors = [];
  let clock = 0;
  let heartbeat;
  const identity = { agentInstallationId: crypto.randomUUID(), agentStreamId: state.agent_stream_id,
    appliedConfigRevision: 'synthetic-config-v1', lastAcknowledgedSourceSeq: 0 };
  const transport = new Transport({ outbox, identity, creatorAccountId: TRAVERSAL_ACCOUNT,
    authTicket: 'synthetic', extensionVersion: '2.0.3', webSocketFactory: () => { throw new Error('Unexpected connection'); },
    monotonicNow: () => clock, onValidationError: error => errors.push(error),
    scheduler: { setTimeout(callback) { heartbeat = callback; return 1; }, clearTimeout() {} } });
  transport.stopped = false;
  transport.socket = { readyState: 1, send: data => sent.push(JSON.parse(data)) };
  transport.session = { creator_account_id: TRAVERSAL_ACCOUNT, connection_id: crypto.randomUUID(), fencing_token: 'synthetic' };
  await transport.flushOutbox();
  transport.syncRequired = snapshotFence;
  transport.startHeartbeat(25_000);
  let notices = 0;
  const drains = [];
  const history = new Coordinator({ outbox,
    ...(immediate ? { onPageCommitted: () => {
      notices++;
      const work = rejectFirstDrain && notices === 1
        ? Promise.reject(new Error('Synthetic delivery unavailable'))
        : transport.flushOutbox();
      drains.push(work.catch(() => undefined));
      return work;
    } } : {}),
    configuration: () => traversalConfiguration({ pages_per_wake: 20, enabled }),
    session: () => ({ ...transport.session, applied_config_revision: identity.appliedConfigRevision }),
    signer: { async read({ operation }) {
      if (operation === 'identity') return { success: true, operation, data: { id: TRAVERSAL_CREATOR } };
      const items = operation === 'conversations'
        ? [{ id: '101', platform_user_id: '101', display_name: null, updated_at: TRAVERSAL_TIME }]
        : [{ id: '201', chat_id: '101', sender_platform_user_id: '101', text: 'Synthetic', sent_at: TRAVERSAL_TIME, direction: 'inbound' }];
      return { success: true, operation, data: { items, continuation: null,
        boundary: operation === 'conversations' ? 'inventory_end' : 'history_start' } };
    } }, delay: async () => {} });
  await history.wake();
  await new Promise(resolve => setImmediate(resolve));
  await Promise.all(drains);
  return { outbox, transport, sent, errors, notices, async tick() {
    clock += 25_000;
    heartbeat();
    await new Promise(resolve => setImmediate(resolve));
  } };
}

for (const twin of twins) {
  test(`${twin.name} heartbeat delivers committed history without passive capture and advances ACK`, async () => {
    const r = await rig(twin);
    const entries = await r.outbox.entries();
    assert.ok(entries.some(item => item.change.evidence?.type === 'generation.started'));
    assert.ok(entries.some(item => item.change.evidence?.type === 'generation.closed'));
    assert.equal(r.sent.length, 0);
    await r.tick();
    const deltas = r.sent.filter(message => message.type === 'ingest.delta');
    assert.equal(deltas.length, 4);
    assert.deepEqual(deltas.map(message => message.payload.source_seq), entries.slice(0, 4).map(item => item.source_seq));
    for (let batch = 0; batch < 2; batch++) {
      const delivered = r.sent.filter(message => message.type === 'ingest.delta');
      await r.transport.dispatch({ type: 'ingest.ack', payload: { committed_source_seq: delivered.at(-1).payload.source_seq,
        snapshot_id: null, snapshot_progress: null } });
    }
    assert.deepEqual(r.sent.filter(message => message.type === 'ingest.delta').map(message => message.payload.source_seq),
      entries.map(item => item.source_seq));
    assert.equal((await r.outbox.entries()).length, 0);
    await r.tick();
    assert.equal(r.sent.filter(message => message.type === 'ingest.delta').length, entries.length);
    assert.equal(r.errors.length, 0);
  });

  test(`${twin.name} committed history requests delivery before the first heartbeat`, async () => {
    const r = await rig(twin, { immediate: true });
    const expected = (await r.outbox.entries()).map(item => item.source_seq);
    assert.ok(r.notices > 0);
    assert.equal(r.sent.filter(message => message.type === 'agent.heartbeat').length, 0);
    const sent = () => r.sent.filter(message => message.type === 'ingest.delta');
    assert.equal(sent().length, 4, 'the existing in-flight limit still applies');
    for (let batch = 0; batch < 3 && (await r.outbox.entries()).length; batch++) {
      await r.transport.dispatch({ type: 'ingest.ack', payload: {
        committed_source_seq: sent().at(-1).payload.source_seq, snapshot_id: null, snapshot_progress: null,
      } });
    }
    assert.deepEqual(sent().map(message => message.payload.source_seq), expected);
    assert.ok(sent().some(message => message.payload.change.type === 'message.upsert'));
    assert.equal((await r.outbox.entries()).length, 0);
    assert.equal(r.sent.filter(message => message.type === 'agent.heartbeat').length, 0);
    assert.equal(r.errors.length, 0);
  });

  test(`${twin.name} committed-page delivery cannot bypass snapshot repair or start disabled history`, async () => {
    const fenced = await rig(twin, { immediate: true, snapshotFence: true });
    assert.ok(fenced.notices > 0);
    assert.equal(fenced.sent.filter(message => message.type === 'ingest.delta').length, 0);
    assert.ok((await fenced.outbox.entries()).length > 0);
    fenced.transport.syncRequired = false;
    await fenced.tick();
    assert.equal(fenced.sent.filter(message => message.type === 'ingest.delta').length, 4);
    const disabled = await rig(twin, { immediate: true, enabled: false });
    assert.equal(disabled.notices, 0);
    assert.equal((await disabled.outbox.entries()).length, 0);
  });

  test(`${twin.name} failed delivery hint does not roll back or retry a committed history page`, async () => {
    const r = await rig(twin, { immediate: true, rejectFirstDrain: true });
    const entries = await r.outbox.entries();
    assert.equal(entries.filter(item => item.change.evidence?.type === 'generation.started').length, 1);
    assert.equal(entries.filter(item => item.change.evidence?.type === 'generation.closed').length, 1);
    assert.equal(entries.filter(item => item.change.type === 'message.upsert').length, 1);
    assert.ok(r.notices > 1);
    assert.equal(r.errors.length, 0);
  });

  test(`${twin.name} heartbeat preserves the snapshot fence before delivering queued history`, async () => {
    const r = await rig(twin);
    r.transport.syncRequired = true;
    await r.tick();
    assert.equal(r.sent.filter(message => message.type === 'ingest.delta').length, 0);
    assert.ok((await r.outbox.entries()).length > 0);
    r.transport.syncRequired = false;
    await r.tick();
    assert.equal(r.sent.filter(message => message.type === 'ingest.delta').length, 4);
  });

  test(`${twin.name} heartbeat reports a failed drain without losing committed history`, async () => {
    const r = await rig(twin);
    const failure = new Error('Synthetic storage failure');
    const readPage = r.outbox.entriesPage;
    r.outbox.entriesPage = async () => { throw failure; };
    await r.tick();
    assert.deepEqual(r.errors, [failure]);
    r.outbox.entriesPage = readPage;
    assert.ok((await r.outbox.entries()).length > 0);
  });
}
