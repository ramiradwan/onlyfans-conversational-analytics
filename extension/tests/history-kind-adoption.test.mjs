import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { gunzipSync, gzipSync } from 'fflate';
import { SIGNER_RELEASE, auditSignerArchive } from '../qualification/signer-release.mjs';
import { normalizeSignerMessage as shipped } from '../transport/read-only-signer-normalization.mjs';
import { normalizeSignerMessage as authoring } from '../transport/signer-normalization.mjs';
import { normalizeSignerConversation as shippedConversation } from '../transport/read-only-signer-normalization.mjs';
import { normalizeSignerConversation as authoringConversation } from '../transport/signer-normalization.mjs';
import { DurableIngestOutbox } from '../transport/read-only-durable-outbox.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';
import { HistoryAcquisitionCoordinator } from '../transport/read-only-history-coordinator.mjs';
import { createSignerReleaseFixture, EXPECTED_ID } from './signer-release-fixture.mjs';

// Synthetic records and bounded observations.
const context = { observedAt: '2026-10-01T00:00:00Z', creatorPlatformId: 'synthetic-account', conversationId: 'synthetic-chat' };
const record = { id: 'synthetic-record', chat_id: context.conversationId,
  sender_platform_user_id: 'synthetic-participant', text: '', sent_at: context.observedAt, direction: 'inbound' };
const metadata = { schema: 'connector-history-kind/v1', mapping_version: 'onlyfans-event-kind/0.2.0',
  evidence_standard: 'client-parity', client_pin_set_version: 'onlyfans-client-parity-pins/2026-10-04.1',
  kind: 'human_message', rule_id: 'CP-H1', evidence_state: 'supported',
  context: { account_id: context.creatorPlatformId, conversation_id: context.conversationId,
    generation_id: 'synthetic-generation', method: 'GET', endpoint: '/api2/v2/chats/{id}/messages', surface: 'native-rest-message-page' },
  source_kind_evidence: { availability: 'available', representations: [{ root: 'list', fields: {
    responseType: { present: true, value_type: 'string', value_token: 'literal-message' },
    systemType: { present: false, value_type: null, value_token: null } } }],
    alias_comparisons: { responseType: 'not-applicable', systemType: 'not-applicable' } } };

test('optional conversation head fields retain the canonical boundary', () => {
  const conversation = { id: context.conversationId, platform_user_id: record.sender_platform_user_id,
    display_name: null, updated_at: context.observedAt, head_message_id: record.id, head_sent_at: context.observedAt };
  for (const normalize of [shippedConversation, authoringConversation]) {
    assert.equal(normalize(conversation, context).chat.chat_id, conversation.id);
    assert.throws(() => normalize({ ...conversation, head_message_id: 1 }, context));
    assert.throws(() => normalize({ ...conversation, head_sent_at: 'synthetic-invalid' }, context));
  }
});

test('reviewed prerelease pin binds all release coordinates', () => {
  assert.equal(SIGNER_RELEASE.version, '0.5.0-rc.1');
  assert.equal(SIGNER_RELEASE.tag, 'v0.5.0-rc.1');
  assert.equal(SIGNER_RELEASE.release_id, 402765184);
  assert.equal(SIGNER_RELEASE.asset_id, 608867698);
  assert.equal(SIGNER_RELEASE.archive, 'local-authenticated-read-connector-0.5.0-rc.1.tgz');
  assert.equal(SIGNER_RELEASE.sha256, '9a4a1143aae37b47b99ed3ac81e4a949d32e17d7785686d0136de9cc859e46a9');
  assert.equal(SIGNER_RELEASE.source_revision, 'd8cc1812b7282097c689ceceed766792c9c1e629');
  assert.equal(SIGNER_RELEASE.source_tree, '37963ea0e3afa03f4613fd9150c64efb5ba920b4');
});

test('archive tampering retains version but never acquires byte authorization', async () => {
  const original = await readFile(new URL(`../vendor/${SIGNER_RELEASE.archive}`, import.meta.url));
  const tar = gunzipSync(original);
  let changed = false;
  for (let offset = 0; offset + 512 <= tar.length;) {
    const name = Buffer.from(tar.subarray(offset, offset + 100)).toString().split('\0')[0];
    const size = Number.parseInt(Buffer.from(tar.subarray(offset + 124, offset + 136)).toString().trim(), 8);
    if (name === 'package/LICENSE') { tar[offset + 512] ^= 1; changed = true; break; }
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  assert.equal(changed, true);
  assert.ok(Buffer.from(tar).includes(Buffer.from(`"version": "${SIGNER_RELEASE.version}"`)));
  const tampered = gzipSync(tar);
  assert.throws(() => auditSignerArchive(tampered), /reviewed release/);
});

test('installed provider delivers native synthetic per-record kinds in both read envelopes', async () => {
  const fixture = createSignerReleaseFixture();
  const provider = await fixture.createProvider();
  await provider.read({ operation: 'identity', refreshMode: 'allow' });
  fixture.reply = () => fixture.response({ hasMore: false, list: [
    { id: '7003', fromUser: { id: '9002' }, chatUserId: '9002', text: '', postedAt: context.observedAt, responseType: 'message', media: [] },
    { id: '7002', fromUser: { id: '9002' }, chatUserId: '9002', text: '', postedAt: context.observedAt, responseType: 'message', systemType: 'call_ended' },
    { id: '7001', fromUser: { id: '9002' }, chatUserId: '9002', text: '', postedAt: context.observedAt, responseType: 'message', systemType: null },
  ] });
  const request = { parameters: { conversationId: '7000' }, refreshMode: 'never' };
  const read = await provider.read({ ...request, operation: 'message-page' });
  assert.equal(read.success, true);
  const changes = read.data.items.map(item => shipped(item, { ...context,
    creatorPlatformId: EXPECTED_ID, conversationId: '7000' }));
  assert.deepEqual(changes.map(change => change.message.event_kind.kind), ['human_message', 'non_message_event', 'unknown']);
  for (const item of read.data.items) {
    assert.equal(item.event_kind.schema, 'connector-history-kind/v1');
    assert.equal(item.event_kind.mapping_version, 'onlyfans-event-kind/0.2.0');
    assert.equal(item.event_kind.evidence_standard, 'client-parity');
  }
  const wrapped = await provider.readHistoryWithEvidence(request);
  assert.deepEqual(wrapped.read.data.items.map(item => item.event_kind.kind), ['human_message', 'non_message_event', 'unknown']);
  assert.ok(wrapped.evidence.items.every(item => item.interpretation.kind === 'unknown'));
});

for (const [name, normalize] of [['shipped', shipped], ['authoring', authoring]]) {
  test(`${name} carries per-record metadata for media-only history`, () => {
    const value = normalize({ ...record, event_kind: structuredClone(metadata) }, context);
    assert.deepEqual(value.message.event_kind, metadata);
    assert.equal(value.message.text.length, 0);
  });
  test(`${name} rejects a detached account or conversation binding`, () => {
    for (const key of ['account_id', 'conversation_id']) {
      const detached = structuredClone(metadata);
      detached.context[key] = 'synthetic-foreign';
      assert.throws(() => normalize({ ...record, event_kind: detached }, context));
    }
  });
}

test('durable merge and snapshot retain kind-only changes and passive replay', async () => {
  const outbox = new DurableIngestOutbox({ storage: new InMemoryIngestionStorage(), creatorAccountId: 'synthetic-partition' });
  await outbox.initialize();
  await outbox.enqueue({ type: 'chat.upsert', chat: { chat_id: context.conversationId, record_kind: 'placeholder',
    platform_user_id: null, display_name: null, updated_at: null } });
  const first = shipped({ ...record, event_kind: structuredClone(metadata) }, context);
  await outbox.enqueue(first);
  const changed = structuredClone(first);
  changed.message.event_kind.evidence_state = 'unsupported';
  changed.message.event_kind.kind = 'unknown';
  await outbox.enqueue(changed);
  await outbox.enqueue(shipped(record, context));
  const entries = await outbox.entries();
  assert.deepEqual(entries.filter(e => e.change.type === 'message.upsert').map(e => e.change.message.event_kind),
    [first.message.event_kind, changed.message.event_kind]);
  const manifest = await outbox.prepareSnapshot();
  const records = [];
  for (let i = 0; i < manifest.chunk_count; i++) records.push(...(await outbox.snapshotChunkFrame(i)).records);
  assert.deepEqual(records.find(r => r.message)?.message.event_kind, changed.message.event_kind);
});

test('shipped coordinator commits every synthetic kind through its durable page path', async () => {
  const outbox = new DurableIngestOutbox({ storage: new InMemoryIngestionStorage(), creatorAccountId: 'synthetic-partition' });
  const state = await outbox.initialize();
  const signer = { async read({ operation }) {
    if (operation === 'identity') return { operation, success: true, data: { id: context.creatorPlatformId } };
    const items = operation === 'conversations'
      ? [{ id: context.conversationId, platform_user_id: record.sender_platform_user_id,
        display_name: null, updated_at: context.observedAt }]
      : ['human_message', 'non_message_event', 'unknown'].map((kind, index) => ({ ...record,
        id: `synthetic-record-${index}`, event_kind: { ...structuredClone(metadata), kind } }));
    return { operation, success: true, data: { items, continuation: null,
      boundary: operation === 'conversations' ? 'inventory_end' : 'history_start' } };
  } };
  const coordinator = new HistoryAcquisitionCoordinator({ outbox, signer, now: () => context.observedAt,
    delay: async () => {}, configuration: () => ({ creator_account_id: 'synthetic-partition', config_revision: 'synthetic-config',
      history_acquisition: { enabled: true, consent_revision: 'synthetic-consent',
        authorized_platform_creator_id: context.creatorPlatformId, recent_window_days: 30,
        page_size: 50, pages_per_wake: 10, request_interval_ms: 0, retry_limit: 2 } }),
    session: () => ({ creator_account_id: 'synthetic-partition', applied_config_revision: 'synthetic-config', account_epoch: state.account_epoch }) });
  assert.equal((await coordinator.wake()).status, 'progressed');
  const entries = (await outbox.entries()).filter(e => e.change.type === 'message.upsert');
  assert.deepEqual(entries.map(e => e.change.message.event_kind.kind), ['human_message', 'non_message_event', 'unknown']);
});
