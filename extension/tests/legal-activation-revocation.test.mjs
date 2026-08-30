import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { ActivationEvidenceStore } from '../runtime/activation-evidence.mjs';
import { ConsentController } from '../runtime/consent-controller.mjs';
import { LegalConsentAuthorization } from '../runtime/legal-consent-authorization.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';
import { emitLegalScenarioEvidence } from './legal-evidence-output.mjs';

const bindings = JSON.parse(await readFile(
  new URL('./fixtures/legal-instrument-bindings.synthetic.json', import.meta.url),
  'utf8',
));

function event() {
  return { addListener() {}, removeListener() {} };
}

function storageArea(values) {
  return {
    async get(keys) {
      return Object.fromEntries(
        keys.filter((key) => Object.hasOwn(values, key)).map((key) => [key, structuredClone(values[key])]),
      );
    },
    async set(update) { Object.assign(values, structuredClone(update)); },
    async clear() { for (const key of Object.keys(values)) delete values[key]; },
  };
}

function consentHarness(activeModeAuthorization) {
  const local = {};
  const session = {};
  const registeredScripts = [];
  const removedPermissions = [];
  const permissionState = { onlyFans: true, localService: true, history: false };
  const chromeApi = {
    runtime: {
      id: 'synthetic-extension-id',
      getURL: (path = '') => `chrome-extension://synthetic-extension-id/${path}`,
      onMessage: event(),
    },
    storage: {
      local: storageArea(local),
      session: storageArea(session),
      onChanged: event(),
    },
    scripting: {
      async getRegisteredContentScripts() { return structuredClone(registeredScripts); },
      async registerContentScripts(scripts) { registeredScripts.push(...structuredClone(scripts)); },
      async unregisterContentScripts({ ids }) {
        for (const id of ids) {
          const index = registeredScripts.findIndex((entry) => entry.id === id);
          if (index >= 0) registeredScripts.splice(index, 1);
        }
      },
    },
    permissions: {
      onRemoved: event(),
      async contains(query) {
        if (query.origins?.includes('https://onlyfans.com/*')) return permissionState.onlyFans;
        if (query.origins?.includes('http://bridge.localhost:17871/*')) return permissionState.localService;
        if (query.permissions) return permissionState.history;
        return false;
      },
      async remove(query) {
        removedPermissions.push(structuredClone(query));
        permissionState.onlyFans = false;
        permissionState.localService = false;
        permissionState.history = false;
        return true;
      },
    },
    alarms: { onAlarm: event(), async create() {} },
    tabs: {
      async query() { return []; },
      async sendMessage() {},
      async reload() {},
    },
  };
  const previewMetrics = {
    async record() {},
    async clear() {},
    async prune() {},
    async summary() {
      return {
        retention_days: 7,
        chat_observations: 0,
        message_observations: 0,
        inbound_observations: 0,
        outbound_observations: 0,
        unknown_direction_observations: 0,
        days: [],
      };
    },
  };
  const bridge = { register() {}, unregister() {} };
  const controller = new ConsentController({
    chromeApi,
    runtime: { async start() {}, async suspend() {} },
    adapter: {
      async loadBrainBinding() { return { creator_account_id: 'synthetic-account' }; },
      async clearBrainBinding() {},
    },
    brainBindingBridge: bridge,
    provisioningIdentityBridge: bridge,
    previewMetrics,
    async clearLocalData() {},
    activeModeAuthorization,
    fetchImpl: async () => ({ ok: true }),
    now: () => new Date('2030-01-08T12:00:00.000Z'),
  });
  return { chromeApi, controller, removedPermissions };
}

test('AE-06 real revoke transition leaves Terms, risk, and Full evidence byte-for-byte unchanged', async () => {
  const indexedDb = new FakeIndexedDb();
  const uuids = [
    '60000000-0000-4000-8000-000000000001',
    '60000000-0000-4000-8000-000000000002',
    '60000000-0000-4000-8000-000000000003',
  ];
  const times = [
    '2026-08-30T10:00:00.000Z',
    '2026-08-30T10:01:00.000Z',
    '2026-08-30T10:02:00.000Z',
  ];
  let uuidIndex = 0;
  let timeIndex = 0;
  const evidenceStore = new ActivationEvidenceStore({
    indexedDb,
    softwareVersion: '2.0.1',
    uuid: () => uuids[uuidIndex++],
    now: () => new Date(times[Math.min(timeIndex++, times.length - 1)]),
  });
  const activeModeAuthorization = new LegalConsentAuthorization({ evidenceStore });
  const { controller, removedPermissions } = consentHarness(activeModeAuthorization);
  const transactionId = '70000000-0000-4000-8000-000000000001';
  const terms = await evidenceStore.recordTermsAcceptance({ transactionId, bindings });
  const risk = await evidenceStore.recordRiskAcknowledgment({ transactionId, bindings });
  const full = await evidenceStore.recordModeChoice({
    transactionId,
    eventType: 'initial_activation',
    selectedMode: 'full',
    termsEventId: terms.event_id,
    riskEventId: risk.event_id,
    bindings,
  });

  await controller.setMode('full', { evidenceEventId: full.event_id });
  assert.equal((await controller.status()).consent.mode, 'full');
  const before = await evidenceStore.exportAuditTrail();

  await controller.setMode('revoked');

  const revokedStatus = await controller.status();
  const after = await evidenceStore.exportAuditTrail();
  assert.equal(revokedStatus.consent.mode, 'revoked');
  assert.deepEqual(after, before);
  assert.equal(removedPermissions.length, 1);
  await emitLegalScenarioEvidence('AE-06', {
    consent_before_revoke: 'full',
    consent_after_revoke: revokedStatus.consent,
    evidence_before_revoke: before,
    evidence_after_revoke: after,
    removed_permissions: removedPermissions,
  });
});
