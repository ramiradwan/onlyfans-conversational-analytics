import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { ActivationEvidenceStore } from '../runtime/activation-evidence.mjs';
import {
  LEGAL_ACTIVATION_FLOW_STORAGE_KEY,
  LegalActivationController,
} from '../runtime/legal-activation-controller.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';
import { emitLegalScenarioEvidence } from './legal-evidence-output.mjs';

const bindings = JSON.parse(await readFile(
  new URL('./fixtures/legal-instrument-bindings.synthetic.json', import.meta.url),
  'utf8',
));

function chromeHarness(initialFlow) {
  const local = { [LEGAL_ACTIVATION_FLOW_STORAGE_KEY]: structuredClone(initialFlow) };
  return {
    local,
    chromeApi: {
      runtime: {
        id: 'synthetic-extension-id',
        getURL: (path = '') => `chrome-extension://synthetic-extension-id/${path}`,
        onMessage: { addListener() {} },
      },
      storage: {
        local: {
          async get(keys) {
            return Object.fromEntries(
              keys.filter((key) => Object.hasOwn(local, key)).map((key) => [key, structuredClone(local[key])]),
            );
          },
          async set(update) { Object.assign(local, structuredClone(update)); },
        },
      },
    },
  };
}

test('AE-07 crash after Full transition recovers the original pending mode_upgrade event', async () => {
  const indexedDb = new FakeIndexedDb();
  const ids = [
    '80000000-0000-4000-8000-000000000001',
    '80000000-0000-4000-8000-000000000002',
    '80000000-0000-4000-8000-000000000003',
    '80000000-0000-4000-8000-000000000004',
  ];
  const times = [
    '2026-08-30T10:00:00.000Z',
    '2026-08-30T10:01:00.000Z',
    '2026-08-30T10:02:00.000Z',
    '2026-08-30T11:00:00.000Z',
  ];
  let idIndex = 0;
  let timeIndex = 0;
  const evidenceStore = new ActivationEvidenceStore({
    indexedDb,
    softwareVersion: '2.0.1',
    uuid: () => ids[idIndex++],
    now: () => new Date(times[Math.min(timeIndex++, times.length - 1)]),
  });
  const initialTransaction = '90000000-0000-4000-8000-000000000001';
  const upgradeTransaction = '90000000-0000-4000-8000-000000000002';
  const terms = await evidenceStore.recordTermsAcceptance({
    transactionId: initialTransaction,
    bindings,
  });
  const risk = await evidenceStore.recordRiskAcknowledgment({
    transactionId: initialTransaction,
    bindings,
  });
  const preview = await evidenceStore.recordModeChoice({
    transactionId: initialTransaction,
    eventType: 'initial_activation',
    selectedMode: 'preview',
    termsEventId: terms.event_id,
    riskEventId: risk.event_id,
    bindings,
  });
  const full = await evidenceStore.recordModeChoice({
    transactionId: upgradeTransaction,
    eventType: 'mode_upgrade',
    selectedMode: 'full',
    termsEventId: terms.event_id,
    riskEventId: risk.event_id,
    bindings,
  });
  const beforeRetry = await evidenceStore.exportAuditTrail();

  const flow = {
    schema: 'ofca-legal-activation-flow/v1',
    transaction_id: upgradeTransaction,
    terms_event_id: terms.event_id,
    risk_event_id: risk.event_id,
    stage: 'mode_selection',
    pending_mode: 'full',
    pending_event_type: 'mode_upgrade',
    completed_mode: null,
    completed_event_id: null,
  };
  const { chromeApi, local } = chromeHarness(flow);
  const consentController = {
    state: { mode: 'full', resume_mode: null },
    setModeCalls: [],
    async status() { return { consent: structuredClone(this.state) }; },
    async setMode(mode, options = {}) {
      this.setModeCalls.push({ mode, options: structuredClone(options) });
      this.state = { mode, resume_mode: null };
      return { consent: structuredClone(this.state) };
    },
  };
  const controller = new LegalActivationController({
    chromeApi,
    consentController,
    evidenceStore,
    bindings: () => bindings,
  });

  const recovered = await controller.chooseMode('full');
  const afterRetry = await evidenceStore.exportAuditTrail();

  assert.equal(recovered.retried, false);
  assert.deepEqual(recovered.evidence, full.envelope);
  assert.deepEqual(afterRetry, beforeRetry);
  assert.equal(consentController.setModeCalls.length, 1);
  assert.deepEqual(consentController.setModeCalls[0], {
    mode: 'full',
    options: { evidenceEventId: full.event_id },
  });
  assert.equal(local[LEGAL_ACTIVATION_FLOW_STORAGE_KEY].pending_mode, null);
  assert.equal(local[LEGAL_ACTIVATION_FLOW_STORAGE_KEY].pending_event_type, null);
  assert.equal(local[LEGAL_ACTIVATION_FLOW_STORAGE_KEY].completed_mode, 'full');
  assert.equal(
    local[LEGAL_ACTIVATION_FLOW_STORAGE_KEY].completed_event_id,
    full.event_id,
  );
  await emitLegalScenarioEvidence('AE-07', {
    crash_boundary: 'mode envelope and consent transition completed before controller completion marker',
    preview_record: preview,
    full_mode_upgrade_record: full,
    audit_before_retry: beforeRetry,
    audit_after_retry: afterRetry,
    recovered_response: recovered,
    recovered_flow: local[LEGAL_ACTIVATION_FLOW_STORAGE_KEY],
  });
});
