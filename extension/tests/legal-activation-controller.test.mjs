import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { ActivationEvidenceStore } from '../runtime/activation-evidence.mjs';
import { LegalActivationController } from '../runtime/legal-activation-controller.mjs';
import { authorizationScope, LegalConsentAuthorization } from '../runtime/legal-consent-authorization.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const bindings = JSON.parse(await readFile(
  new URL('./fixtures/legal-instrument-bindings.synthetic.json', import.meta.url),
  'utf8',
));

function storageArea(values) {
  return {
    async get(keys) {
      return Object.fromEntries(
        keys.filter((key) => Object.hasOwn(values, key)).map((key) => [key, structuredClone(values[key])]),
      );
    },
    async set(update) { Object.assign(values, structuredClone(update)); },
  };
}

function modeRecord(mode, presentedBindings = bindings) {
  const occurredAt = '2026-08-30T10:02:00.000Z';
  return {
    schema: 'ofca-mode-legal-evidence/v2',
    authorization_scope: authorizationScope(presentedBindings, mode),
    record_type: 'mode_envelope',
    event_id: '50000000-0000-4000-8000-000000000099',
    envelope: {
      schema_version: '2.0',
      event_id: '50000000-0000-4000-8000-000000000099',
      event_type: 'initial_activation',
      occurred_at: occurredAt,
      software_version: '2.0.1',
      selected_mode: mode,
      locale: 'en',
      actions: {
        terms: { action: 'accepted', timestamp: occurredAt },
        risk_disclosure: { action: 'acknowledged', timestamp: occurredAt },
        extension_data_handling: {
          action: mode === 'full' ? 'affirmatively_authorized' : 'preview_only',
          timestamp: occurredAt,
        },
      },
      presented_instruments: structuredClone(presentedBindings.instruments),
    },
  };
}

function policyHarness() {
  const records = new Map();
  const stored = {};
  const bindingRef = { current: structuredClone(bindings) };
  const evidenceStore = {
    async event(id) { return records.get(id) ?? null; },
  };
  return {
    bindingRef,
    records,
    stored,
    policy: new LegalConsentAuthorization({
      evidenceStore,
      bindings: () => bindingRef.current,
      storage: storageArea(stored),
      now: () => new Date('2030-01-08T12:00:00.000Z'),
    }),
  };
}

function activationHarness() {
  const local = {};
  const bindingRef = { current: structuredClone(bindings) };
  const chromeApi = {
    runtime: {
      id: 'synthetic-extension-id',
      getURL: (path = '') => `chrome-extension://synthetic-extension-id/${path}`,
      onMessage: { addListener() {} },
    },
    storage: {
      local: storageArea(local),
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
    '50000000-0000-4000-8000-000000000006',
  ];
  const times = [
    '2026-08-30T10:00:00.000Z',
    '2026-08-30T10:01:00.000Z',
    '2026-08-30T10:02:00.000Z',
    '2026-08-30T10:03:00.000Z',
    '2026-08-30T10:04:00.000Z',
    '2026-08-30T10:05:00.000Z',
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
    bindings: () => bindingRef.current,
  });
  return { bindingRef, controller, consentController, evidenceStore, local };
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

test('active transition authorization binds persisted evidence to the exact requested mode', async () => {
  const h = policyHarness();
  h.records.set('50000000-0000-4000-8000-000000000099', modeRecord('full'));
  assert.equal(await h.policy.authorizeTransition({
    currentState: { mode: 'off' },
    requestedMode: 'full',
    evidenceEventId: '50000000-0000-4000-8000-000000000099',
  }), true);
  assert.equal(await h.policy.authorizeTransition({
    currentState: { mode: 'off' },
    requestedMode: 'preview',
    evidenceEventId: '50000000-0000-4000-8000-000000000099',
  }), false);
});

test('resume and reconciliation require evidence authorized for the current disclosure scope', async () => {
  const h = policyHarness();
  h.records.set('50000000-0000-4000-8000-000000000099', modeRecord('full'));
  assert.equal(await h.policy.authorizeTransition({
    currentState: { mode: 'off' },
    requestedMode: 'full',
    evidenceEventId: '50000000-0000-4000-8000-000000000099',
  }), true);
  assert.equal(await h.policy.authorizeResume({ resumeMode: 'full', currentState: { mode: 'paused', authorization_event_id: '50000000-0000-4000-8000-000000000099' } }), true);
  assert.equal(await h.policy.reconcileActiveMode({ mode: 'full', state: { authorization_event_id: '50000000-0000-4000-8000-000000000099' } }), true);

  h.bindingRef.current = structuredClone(bindings);
  h.bindingRef.current.instruments.extension_privacy_notice = {
    ...h.bindingRef.current.instruments.extension_privacy_notice,
    version: '1.1.0',
    rendered_sha256: 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
  };
  assert.equal(await h.policy.authorizeResume({ resumeMode: 'full', currentState: { mode: 'paused', authorization_event_id: '50000000-0000-4000-8000-000000000099' } }), false);
  assert.equal(await h.policy.reconcileActiveMode({ mode: 'full', state: { authorization_event_id: '50000000-0000-4000-8000-000000000099' } }), false);
});

test('changed Terms reset only stale pre-mode evidence and require a fresh activation choice', async () => {
  const h = activationHarness();
  await h.controller.acceptTerms();
  await h.controller.acknowledgeRisk();
  await h.controller.activateSoftware();
  const before = await h.controller.status();
  assert.notEqual(before.flow.terms_event_id, null);
  assert.notEqual(before.flow.risk_event_id, null);
  assert.equal(before.flow.stage, 'mode_selection');

  h.bindingRef.current = structuredClone(bindings);
  h.bindingRef.current.instruments.terms_of_service = {
    ...h.bindingRef.current.instruments.terms_of_service,
    version: '2.1.0',
    rendered_sha256: 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
  };

  const after = await h.controller.status();
  assert.equal(after.flow.terms_event_id, null);
  assert.equal(after.flow.risk_event_id, before.flow.risk_event_id);
  assert.equal(after.flow.stage, 'pre_mode');
  assert.equal(after.flow.completed_mode, null);
  assert.equal(after.flow.completed_event_id, null);
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
