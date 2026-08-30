import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { ActivationEvidenceStore } from '../runtime/activation-evidence.mjs';
import { LegalActivationController } from '../runtime/legal-activation-controller.mjs';
import { LegalConsentAuthorization } from '../runtime/legal-consent-authorization.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const bindings = JSON.parse(await readFile(
  new URL('./fixtures/legal-instrument-bindings.synthetic.json', import.meta.url),
  'utf8',
));

function policyHarness() {
  const records = new Map();
  const evidenceStore = {
    async event(id) { return records.get(id) ?? null; },
    async modeEvidenceExists(mode) {
      return [...records.values()].some((record) => record.envelope?.selected_mode === mode);
    },
  };
  return {
    records,
    policy: new LegalConsentAuthorization({ evidenceStore }),
  };
}

function activationHarness() {
  const local = {};
  const chromeApi = {
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
  };
  const consentController = {
    state: { mode: 'off', resume_mode: null },
    setModeCalls: [],
    async status() { return { consent: structuredClone(this.state) }; },
    async setMode(mode, options = {}) {
      this.setModeCalls.push({ mode, options: structuredClone(options) });
      this.state = { mode, resume_mode: null };
      return { consent: structuredClone(this.state) };
    },
  };
  let uuidIndex = 0;
  let timeIndex = 0;
  const uuids = [
    '50000000-0000-4000-8000-000000000001',
    '50000000-0000-4000-8000-000000000002',
    '50000000-0000-4000-8000-000000000003',
    '50000000-0000-4000-8000-000000000004',
    '50000000-0000-4000-8000-000000000005',
  ];
  const times = [
    '2026-08-30T10:00:00.000Z',
    '2026-08-30T10:01:00.000Z',
    '2026-08-30T10:02:00.000Z',
    '2026-08-30T10:03:00.000Z',
    '2026-08-30T10:04:00.000Z',
  ];
  const evidenceStore = new ActivationEvidenceStore({
    indexedDb: new FakeIndexedDb(),
    softwareVersion: '2.0.1',
    uuid: () => uuids[uuidIndex++],
    now: () => new Date(times[Math.min(timeIndex++, times.length - 1)]),
  });
  const controller = new LegalActivationController({
    chromeApi,
    consentController,
    evidenceStore,
    bindings: () => bindings,
  });
  return { controller, consentController, evidenceStore, local };
}

test('active Preview/Full transitions fail closed without matching persisted evidence', async () => {
  const h = policyHarness();
  assert.equal(await h.policy.authorizeTransition({
    currentState: { mode: 'off' },
    requestedMode: 'preview',
    evidenceEventId: null,
  }), false);
  assert.equal(await h.policy.authorizeTransition({
    currentState: { mode: 'off' },
    requestedMode: 'full',
    evidenceEventId: 'missing-event',
  }), false);
});

test('active transition authorization binds the persisted evidence to the exact requested mode', async () => {
  const h = policyHarness();
  h.records.set('full-event', {
    record_type: 'mode_envelope',
    envelope: { selected_mode: 'full' },
  });
  assert.equal(await h.policy.authorizeTransition({
    currentState: { mode: 'off' },
    requestedMode: 'full',
    evidenceEventId: 'full-event',
  }), true);
  assert.equal(await h.policy.authorizeTransition({
    currentState: { mode: 'off' },
    requestedMode: 'preview',
    evidenceEventId: 'full-event',
  }), false);
});

test('resume and reconciliation require existing evidence for the active mode', async () => {
  const h = policyHarness();
  assert.equal(await h.policy.authorizeResume({ resumeMode: 'full' }), false);
  assert.equal(await h.policy.reconcileActiveMode({ mode: 'full' }), false);
  h.records.set('full-event', {
    record_type: 'mode_envelope',
    envelope: { selected_mode: 'full' },
  });
  assert.equal(await h.policy.authorizeResume({ resumeMode: 'full' }), true);
  assert.equal(await h.policy.reconcileActiveMode({ mode: 'full' }), true);
});

test('AE-07 lost-response retry reuses the same mode envelope and timestamp', async () => {
  const h = activationHarness();
  await h.controller.acceptTerms();
  await h.controller.acknowledgeRisk();
  await h.controller.activateSoftware();

  const first = await h.controller.chooseMode('preview');
  const beforeRetry = await h.evidenceStore.exportAuditTrail();
  const second = await h.controller.chooseMode('preview');
  const afterRetry = await h.evidenceStore.exportAuditTrail();

  assert.equal(first.retried, false);
  assert.equal(second.retried, true);
  assert.deepEqual(second.evidence, first.evidence);
  assert.equal(second.evidence.event_id, first.evidence.event_id);
  assert.equal(second.evidence.occurred_at, first.evidence.occurred_at);
  assert.deepEqual(afterRetry, beforeRetry);
  assert.equal(afterRetry.filter((record) => record.record_type === 'mode_envelope').length, 1);
  assert.equal(h.consentController.setModeCalls.length, 2);
  assert.equal(h.consentController.setModeCalls[0].mode, 'preview');
  assert.equal(
    h.consentController.setModeCalls[0].options.evidenceEventId,
    first.evidence.event_id,
  );
  assert.deepEqual(h.consentController.setModeCalls[1], h.consentController.setModeCalls[0]);
});
