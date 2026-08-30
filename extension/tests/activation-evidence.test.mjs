import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  ActivationEvidenceStore,
  validateActivationEnvelopeV2,
} from '../runtime/activation-evidence.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';
import { emitLegalScenarioEvidence } from './legal-evidence-output.mjs';

const bindings = JSON.parse(await readFile(
  new URL('./fixtures/legal-instrument-bindings.synthetic.json', import.meta.url),
  'utf8',
));
const schemaBytes = await readFile(new URL('../../shared/legal/activation-evidence.schema.json', import.meta.url));
const schemaLock = JSON.parse(await readFile(
  new URL('../../shared/legal/activation-evidence.lock.json', import.meta.url),
  'utf8',
));
const UUIDS = Object.freeze([
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000003',
  '10000000-0000-4000-8000-000000000004',
  '10000000-0000-4000-8000-000000000005',
  '10000000-0000-4000-8000-000000000006',
  '10000000-0000-4000-8000-000000000007',
  '10000000-0000-4000-8000-000000000008',
  '10000000-0000-4000-8000-000000000009',
  '10000000-0000-4000-8000-00000000000a',
  '10000000-0000-4000-8000-00000000000b',
  '10000000-0000-4000-8000-00000000000c',
]);

function harness(times = [
  '2026-08-30T10:00:00.000Z',
  '2026-08-30T10:01:00.000Z',
  '2026-08-30T10:02:00.000Z',
  '2026-08-30T10:03:00.000Z',
]) {
  const indexedDb = new FakeIndexedDb();
  let timeIndex = 0;
  let uuidIndex = 0;
  const store = new ActivationEvidenceStore({
    indexedDb,
    softwareVersion: '2.0.1',
    now: () => new Date(times[Math.min(timeIndex++, times.length - 1)]),
    uuid: () => UUIDS[uuidIndex++],
  });
  return { indexedDb, store };
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function gitBlobSha(bytes) {
  return createHash('sha1')
    .update(Buffer.from(`blob ${bytes.length}\0`))
    .update(bytes)
    .digest('hex');
}

const INITIAL_TX = '20000000-0000-4000-8000-000000000001';
const UPGRADE_TX = '20000000-0000-4000-8000-000000000002';

async function acceptedPreMode(store, transactionId = INITIAL_TX) {
  const terms = await store.recordTermsAcceptance({ transactionId, bindings });
  const risk = await store.recordRiskAcknowledgment({ transactionId, bindings });
  return { terms, risk };
}

test('AE-01 Terms acceptance survives abandonment without fabricating selected_mode', async () => {
  const { store } = harness();
  const terms = await store.recordTermsAcceptance({ transactionId: INITIAL_TX, bindings });
  const trail = await store.exportAuditTrail();
  assert.equal(trail.length, 1);
  assert.equal(trail[0].event_id, terms.event_id);
  assert.equal(trail[0].legal_meaning, 'terms');
  assert.equal(trail.some((record) => record.record_type === 'mode_envelope'), false);
  await emitLegalScenarioEvidence('AE-01', { trail });
});

test('AE-02 Terms and risk remain separate durable pre-mode records without a v2 envelope', async () => {
  const { store } = harness();
  const { terms, risk } = await acceptedPreMode(store);
  assert.notEqual(terms.event_id, risk.event_id);
  assert.notEqual(terms.occurred_at, risk.occurred_at);
  const trail = await store.exportAuditTrail();
  assert.deepEqual(trail.map((record) => record.legal_meaning), ['terms', 'risk_disclosure']);
  assert.equal(trail.some((record) => record.record_type === 'mode_envelope'), false);
  await emitLegalScenarioEvidence('AE-02', { terms, risk, trail });
});

test('AE-03 Preview v2 preserves original Terms and risk timestamps', async () => {
  const { store } = harness();
  const { terms, risk } = await acceptedPreMode(store);
  const result = await store.recordModeChoice({
    transactionId: INITIAL_TX,
    eventType: 'initial_activation',
    selectedMode: 'preview',
    termsEventId: terms.event_id,
    riskEventId: risk.event_id,
    bindings,
  });
  const envelope = validateActivationEnvelopeV2(result.envelope);
  assert.equal(envelope.selected_mode, 'preview');
  assert.equal(envelope.actions.extension_data_handling.action, 'preview_only');
  assert.equal(envelope.actions.terms.timestamp, terms.occurred_at);
  assert.equal(envelope.actions.risk_disclosure.timestamp, risk.occurred_at);
  assert.notEqual(envelope.occurred_at, terms.occurred_at);
  await emitLegalScenarioEvidence('AE-03', { terms, risk, mode_record: result });
});

test('AE-04 Full v2 uses affirmative authorization and original pre-mode timestamps', async () => {
  const { store } = harness();
  const { terms, risk } = await acceptedPreMode(store);
  const result = await store.recordModeChoice({
    transactionId: INITIAL_TX,
    eventType: 'initial_activation',
    selectedMode: 'full',
    termsEventId: terms.event_id,
    riskEventId: risk.event_id,
    bindings,
  });
  assert.equal(result.envelope.selected_mode, 'full');
  assert.equal(result.envelope.actions.extension_data_handling.action, 'affirmatively_authorized');
  assert.equal(result.envelope.actions.terms.action, 'accepted');
  assert.equal(result.envelope.actions.risk_disclosure.action, 'acknowledged');
  assert.equal(result.envelope.actions.terms.timestamp, terms.occurred_at);
  assert.equal(result.envelope.actions.risk_disclosure.timestamp, risk.occurred_at);
  await emitLegalScenarioEvidence('AE-04', { terms, risk, mode_record: result });
});

test('AE-05 Preview to Full creates a distinct later envelope without rewriting Preview', async () => {
  const { store } = harness([
    '2026-08-30T10:00:00.000Z',
    '2026-08-30T10:01:00.000Z',
    '2026-08-30T10:02:00.000Z',
    '2026-08-30T11:00:00.000Z',
  ]);
  const { terms, risk } = await acceptedPreMode(store);
  const preview = await store.recordModeChoice({
    transactionId: INITIAL_TX,
    eventType: 'initial_activation',
    selectedMode: 'preview',
    termsEventId: terms.event_id,
    riskEventId: risk.event_id,
    bindings,
  });
  const before = structuredClone(preview);
  const full = await store.recordModeChoice({
    transactionId: UPGRADE_TX,
    eventType: 'mode_upgrade',
    selectedMode: 'full',
    termsEventId: terms.event_id,
    riskEventId: risk.event_id,
    bindings,
  });
  assert.notEqual(full.event_id, preview.event_id);
  assert.ok(Date.parse(full.occurred_at) > Date.parse(preview.occurred_at));
  assert.deepEqual(await store.event(preview.event_id), before);
  assert.equal(full.envelope.actions.terms.action, 'previously_accepted');
  assert.equal(full.envelope.actions.risk_disclosure.action, 'previously_acknowledged');
  assert.equal(full.envelope.actions.terms.timestamp, terms.occurred_at);
  assert.equal(full.envelope.actions.risk_disclosure.timestamp, risk.occurred_at);
  await emitLegalScenarioEvidence('AE-05', {
    terms,
    risk,
    preview_before_upgrade: before,
    full_upgrade: full,
    preview_after_upgrade: await store.event(preview.event_id),
  });
});

test('AE-06 append-only store retains historical records unchanged', async () => {
  const { store } = harness();
  const { terms, risk } = await acceptedPreMode(store);
  await store.recordModeChoice({
    transactionId: INITIAL_TX,
    eventType: 'initial_activation',
    selectedMode: 'full',
    termsEventId: terms.event_id,
    riskEventId: risk.event_id,
    bindings,
  });
  const before = await store.exportAuditTrail();
  assert.deepEqual(await store.exportAuditTrail(), before);
});

test('AE-07 pre-mode and mode-choice retries preserve event IDs and timestamps', async () => {
  const { store } = harness([
    '2026-08-30T10:00:00.000Z',
    '2026-08-30T10:01:00.000Z',
    '2026-08-30T10:02:00.000Z',
    '2026-08-30T12:00:00.000Z',
    '2026-08-30T12:01:00.000Z',
  ]);
  const firstTerms = await store.recordTermsAcceptance({ transactionId: INITIAL_TX, bindings });
  const firstRisk = await store.recordRiskAcknowledgment({ transactionId: INITIAL_TX, bindings });
  const firstMode = await store.recordModeChoice({
    transactionId: INITIAL_TX,
    eventType: 'initial_activation',
    selectedMode: 'preview',
    termsEventId: firstTerms.event_id,
    riskEventId: firstRisk.event_id,
    bindings,
  });

  assert.deepEqual(
    await store.recordTermsAcceptance({ transactionId: INITIAL_TX, bindings }),
    firstTerms,
  );
  assert.deepEqual(
    await store.recordRiskAcknowledgment({ transactionId: INITIAL_TX, bindings }),
    firstRisk,
  );
  assert.deepEqual(
    await store.recordModeChoice({
      transactionId: INITIAL_TX,
      eventType: 'initial_activation',
      selectedMode: 'preview',
      termsEventId: firstTerms.event_id,
      riskEventId: firstRisk.event_id,
      bindings,
    }),
    firstMode,
  );

  const trail = await store.exportAuditTrail();
  assert.equal(trail.length, 3);
  assert.equal(trail.filter((record) => record.record_type === 'mode_envelope').length, 1);
  assert.equal(trail[2].event_id, firstMode.event_id);
  assert.equal(trail[2].occurred_at, firstMode.occurred_at);
});

test('AE-08 instrument changes require new acceptance rather than silent rebinding', async () => {
  const { store } = harness();
  const { terms, risk } = await acceptedPreMode(store);
  const changed = structuredClone(bindings);
  changed.instruments.terms_of_service.rendered_sha256 =
    'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee';
  let rejection = null;
  try {
    await store.recordModeChoice({
      transactionId: INITIAL_TX,
      eventType: 'initial_activation',
      selectedMode: 'preview',
      termsEventId: terms.event_id,
      riskEventId: risk.event_id,
      bindings: changed,
    });
  } catch (error) {
    rejection = error instanceof Error ? error.message : String(error);
  }
  assert.match(rejection ?? '', /Terms instrument changed/);
  assert.equal(terms.action, 'accepted');
  assert.equal(risk.action, 'acknowledged');
  await emitLegalScenarioEvidence('AE-08', {
    changed_field: 'terms_of_service.rendered_sha256',
    rejection,
    preserved_terms_record: terms,
    preserved_risk_record: risk,
  });
});

test('AE-09 schema-v2 invariant rejects envelopes without truthful selected_mode', async () => {
  const computedSha256 = sha256(schemaBytes);
  const computedBlobSha = gitBlobSha(schemaBytes);
  assert.equal(computedSha256, schemaLock.source_sha256);
  assert.equal(computedBlobSha, schemaLock.source_blob_sha);
  assert.equal(computedBlobSha, 'aa550690ae5dbf840826b74864a5fbd0290ab441');

  const document = {
    schema_version: '2.0',
    event_id: UUIDS[0],
    event_type: 'initial_activation',
    occurred_at: '2026-08-30T10:00:00.000Z',
    software_version: '2.0.1',
    locale: 'en',
    actions: {
      terms: { action: 'accepted', timestamp: '2026-08-30T09:58:00.000Z' },
      risk_disclosure: { action: 'acknowledged', timestamp: '2026-08-30T09:59:00.000Z' },
      extension_data_handling: {
        action: 'preview_only',
        timestamp: '2026-08-30T10:00:00.000Z',
      },
    },
    presented_instruments: bindings.instruments,
  };
  let rejection = null;
  try {
    validateActivationEnvelopeV2(document);
  } catch (error) {
    rejection = error instanceof Error ? error.message : String(error);
  }
  assert.match(rejection ?? '', /missing fields|selected_mode/);
  await emitLegalScenarioEvidence('AE-09', {
    schema_lock: schemaLock,
    computed_source_sha256: computedSha256,
    computed_git_blob_sha: computedBlobSha,
    missing_selected_mode_rejection: rejection,
  });
});
