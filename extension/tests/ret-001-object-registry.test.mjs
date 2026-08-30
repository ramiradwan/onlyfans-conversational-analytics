import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  RET001_NO_ENGINEERING_MINIMUM,
  canonicalRet001RegistryJson,
  loadRet001Registry,
  validateRet001Registry,
} from '../../tools/retention-evidence/ret-001-registry.mjs';

const EXPECTED_OBJECT_IDS = Object.freeze([
  'COMP-ANALYTICS-GRAPH','COMP-ANALYTICS-PROJECTION','COMP-ANALYTICS-PROJECTION-STATE',
  'COMP-AUTH-DATASET-STATE','COMP-BACKUP-CANONICAL','COMP-BACKUP-PROJECTIONS',
  'COMP-BRIDGE-DERIVED-STATE','COMP-BRIDGE-PROJECTION-MESSAGES','COMP-BRIDGE-PROJECTION-STATE',
  'COMP-CANONICAL-CHATS','COMP-CANONICAL-CONTROL-STATE','COMP-CANONICAL-ENTITY-AUDIT-STATE',
  'COMP-CANONICAL-MESSAGES','COMP-CANONICAL-OPERATIONAL-STATE','COMP-CANONICAL-PROJECTION-COORDINATION',
  'COMP-CANONICAL-SNAPSHOT-STAGING','COMP-CANONICAL-WAL-SHM','COMP-HISTORY-COVERAGE-STATE',
  'COMP-LOCAL-KEY-MATERIAL','COMP-MIGRATION-BACKUP','COMP-RAW-INGEST-EVENTS','COMP-RESTORE-TEMP',
  'EXT-ACCOUNT-META','EXT-BROWSER-BINDING-STATE','EXT-BROWSER-CONTROL-STATE','EXT-CHATS',
  'EXT-COMMAND-RESULTS','EXT-CONFIG','EXT-COVERAGE-EVIDENCE','EXT-CREDENTIALS',
  'EXT-ENCRYPTION-KEY-CHECK','EXT-HISTORY-JOBS','EXT-LEGAL-EVIDENCE','EXT-MESSAGES',
  'EXT-OUTBOX','EXT-SNAPSHOT-CHUNKS','EXT-SNAPSHOT-MANIFESTS','EXT-SNAPSHOT-OVERRIDES',
]);

test('RET-001 canonical registry package resolves deterministically without reconciliation scaffolding', async () => {
  const { registry, text, sourceText } = await loadRet001Registry();
  const canonical = canonicalRet001RegistryJson(registry);
  assert.equal(canonicalRet001RegistryJson(JSON.parse(canonical)), canonical);
  assert.equal(text, canonical);
  assert.ok(text.endsWith('\n'));

  const source = JSON.parse(sourceText);
  assert.equal(source.schema, 'ofca-ret-001-object-registry-package/v1');
  assert.equal(source.inventory_status, 'COMPLETE');
  assert.ok(Array.isArray(source.object_catalog_files));
  assert.ok(source.object_catalog_files.length > 0);
  assert.equal(new Set(source.object_catalog_files).size, source.object_catalog_files.length);
  for (const file of source.object_catalog_files) {
    assert.equal(file.includes('seed'), false, `${file} must not encode workstream seed history`);
    assert.equal(file.includes('fragment'), false, `${file} must not encode overlay fragment history`);
  }

  const packageUrl = new URL('../../tools/retention-evidence/ret-001-objects.json', import.meta.url);
  const packagedObjects = [];
  for (const file of source.object_catalog_files) {
    const catalog = JSON.parse(await readFile(new URL(file, packageUrl), 'utf8'));
    assert.ok(Array.isArray(catalog) && catalog.length > 0, `${file} must be a direct non-empty object catalog`);
    packagedObjects.push(...catalog);
  }
  assert.equal(packagedObjects.length, 38);
  assert.deepEqual(packagedObjects.map((entry) => entry.id).sort(), [...EXPECTED_OBJECT_IDS].sort());
  assert.deepEqual(registry.objects.map((entry) => entry.id), [...EXPECTED_OBJECT_IDS].sort());

  const digest = createHash('sha256').update(text).digest('hex');
  assert.match(digest, /^[0-9a-f]{64}$/);
});

test('RET-001 complete registry cannot select Product or Legal retention policy', async () => {
  const { registry } = await loadRet001Registry();
  assert.equal(registry.inventory_status, 'COMPLETE');
  assert.equal(registry.scope.production_lifecycle_changes_authorized, false);
  assert.equal(registry.scope.phase, 'characterization');
  for (const entry of registry.objects) {
    assert.equal(entry.raw_text_requirement.minimum_operational_retention, RET001_NO_ENGINEERING_MINIMUM);
    assert.equal(entry.legal_policy.status, 'UNRESOLVED');
    assert.equal(entry.legal_policy.retention_period, 'UNRESOLVED');
    assert.equal(entry.legal_policy.required_deletion_behavior, 'UNRESOLVED');
    assert.equal(entry.legal_policy.minimum_retention, RET001_NO_ENGINEERING_MINIMUM);
    assert.equal(entry.engineering_design_options.status, 'not_selected');
    assert.ok(entry.evidence_ids.length > 0);
  }
});

test('RET-001 Phase-3 raw-text fact is reflected in canonical Bridge derived-state object', async () => {
  const { registry } = await loadRet001Registry();
  const bridge = registry.objects.find((entry) => entry.id === 'COMP-BRIDGE-DERIVED-STATE');
  assert.equal(bridge.raw_text_requirement.contains_raw_message_text, true);
  assert.ok(bridge.source_references.includes('app/persistence/history.py'));
});

test('RET-001 reconstruction closure paths remain exact after registry flattening', async () => {
  const { registry } = await loadRet001Registry();
  const byId = new Map(registry.objects.map((entry) => [entry.id, entry]));
  assert.ok(byId.get('COMP-CANONICAL-SNAPSHOT-STAGING').reconstruction.copies_to_object_ids.includes('COMP-CANONICAL-MESSAGES'));
  const restoreTargets = byId.get('COMP-RESTORE-TEMP').reconstruction.copies_to_object_ids;
  assert.ok(restoreTargets.includes('COMP-ANALYTICS-PROJECTION'));
  assert.ok(restoreTargets.includes('COMP-ANALYTICS-PROJECTION-STATE'));
  assert.ok(restoreTargets.includes('COMP-ANALYTICS-GRAPH'));
  assert.equal(restoreTargets.includes('COMP-BRIDGE-PROJECTION-MESSAGES'), false);
  assert.ok(byId.get('EXT-SNAPSHOT-OVERRIDES').reconstruction.copies_to_object_ids.includes('EXT-SNAPSHOT-CHUNKS'));
});

test('RET-001 object registry rejects premature Legal policy selection', async () => {
  const { registry } = await loadRet001Registry();
  const mutated = structuredClone(registry);
  mutated.objects[0].legal_policy.retention_period = '90 days';
  assert.throws(() => validateRet001Registry(mutated), /legal_policy\.retention_period must remain UNRESOLVED/);
});

test('RET-001 object registry rejects unsupported engineering retention minimums', async () => {
  const { registry } = await loadRet001Registry();
  const mutated = structuredClone(registry);
  mutated.objects[0].raw_text_requirement.minimum_operational_retention = '30 days';
  assert.throws(() => validateRet001Registry(mutated), /exact engineering minimum text/);
});

test('RET-001 reconstruction edges must reference registered stable object IDs', async () => {
  const { registry } = await loadRet001Registry();
  const mutated = structuredClone(registry);
  mutated.objects[0].reconstruction.derived_from_object_ids.push('COMP-NOT-REGISTERED');
  assert.throws(() => validateRet001Registry(mutated), /references unknown object COMP-NOT-REGISTERED/);
});
