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

test('RET-001 object registry is deterministic and Phase-1 complete without selecting policy', async () => {
  const { registry, text } = await loadRet001Registry();
  const canonical = canonicalRet001RegistryJson(registry);
  assert.equal(canonicalRet001RegistryJson(JSON.parse(canonical)), canonical);
  assert.ok(text.endsWith('\n'));
  assert.equal(registry.inventory_status, 'COMPLETE');
  assert.equal(registry.scope.production_lifecycle_changes_authorized, false);
  assert.equal(registry.scope.phase, 'characterization');
  for (const entry of registry.objects) {
    assert.equal(entry.raw_text_requirement.minimum_operational_retention, RET001_NO_ENGINEERING_MINIMUM);
    assert.equal(entry.legal_policy.status, 'UNRESOLVED');
    assert.equal(entry.legal_policy.retention_period, 'UNRESOLVED');
    assert.equal(entry.legal_policy.required_deletion_behavior, 'UNRESOLVED');
    assert.equal(entry.engineering_design_options.status, 'not_selected');
    assert.ok(entry.evidence_ids.length > 0);
  }
  const digest = createHash('sha256').update(text).digest('hex');
  assert.match(digest, /^[0-9a-f]{64}$/);
});

test('RET-001 overlay seed provenance is bound to the exact Git blob bytes', async () => {
  const overlayUrl = new URL('../../tools/retention-evidence/ret-001-objects.json', import.meta.url);
  const overlay = JSON.parse(await readFile(overlayUrl, 'utf8'));
  const seed = await readFile(new URL(overlay.seed_file, overlayUrl));
  const gitBlobSha = createHash('sha1')
    .update(`blob ${seed.length}\0`)
    .update(seed)
    .digest('hex');
  assert.equal(gitBlobSha, overlay.seed_blob_sha);
});

test('RET-001 completed inventory preserves prior objects and adds bounded closure objects', async () => {
  const { registry } = await loadRet001Registry();
  const ids = new Set(registry.objects.map((entry) => entry.id));
  assert.equal(registry.objects.length, 38);
  for (const required of [
    'EXT-MESSAGES','EXT-OUTBOX','EXT-HISTORY-JOBS','EXT-SNAPSHOT-CHUNKS','EXT-SNAPSHOT-OVERRIDES',
    'EXT-BROWSER-BINDING-STATE','EXT-BROWSER-CONTROL-STATE','EXT-LEGAL-EVIDENCE',
    'COMP-CANONICAL-MESSAGES','COMP-RAW-INGEST-EVENTS','COMP-CANONICAL-SNAPSHOT-STAGING',
    'COMP-BRIDGE-PROJECTION-MESSAGES','COMP-BRIDGE-DERIVED-STATE','COMP-BRIDGE-PROJECTION-STATE',
    'COMP-ANALYTICS-PROJECTION','COMP-ANALYTICS-PROJECTION-STATE','COMP-ANALYTICS-GRAPH',
    'COMP-BACKUP-CANONICAL','COMP-BACKUP-PROJECTIONS','COMP-MIGRATION-BACKUP','COMP-RESTORE-TEMP',
    'COMP-CANONICAL-WAL-SHM','COMP-AUTH-DATASET-STATE','COMP-LOCAL-KEY-MATERIAL',
  ]) assert.ok(ids.has(required), `missing required RET-001 object ${required}`);
});

test('RET-001 Phase-3 raw-text fact is reflected in canonical Bridge derived-state object', async () => {
  const { registry } = await loadRet001Registry();
  const bridge = registry.objects.find((entry) => entry.id === 'COMP-BRIDGE-DERIVED-STATE');
  assert.equal(bridge.raw_text_requirement.contains_raw_message_text, true);
  assert.ok(bridge.source_references.includes('app/persistence/history.py'));
});

test('RET-001 reconstruction path gaps are closed for snapshot staging, restore staging, and snapshot overrides', async () => {
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
