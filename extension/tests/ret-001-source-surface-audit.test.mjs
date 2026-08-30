import assert from 'node:assert/strict';
import test from 'node:test';

import { loadRet001Registry } from '../../tools/retention-evidence/ret-001-registry.mjs';
import {
  RET001_AGGREGATE_SURFACE_OBJECTS,
  assertRet001SourceSurfaceCompleteness,
  buildRet001SourceSurfaceAudit,
  discoverRet001ApplicationSurfaces,
} from '../../tools/retention-evidence/ret-001-source-surface-audit.mjs';

test('RET-001 Phase-1 source audit discovers schema and bounded non-schema persistence surfaces', async () => {
  const surfaces = await discoverRet001ApplicationSurfaces();
  const ids = new Set(surfaces.map((surface) => surface.id));
  for (const expected of [
    'idb:full-account:messages',
    'idb:ofca_legal_evidence_v1:records',
    'sql:auth.sqlite3:authorized_account_bindings',
    'sql:auth.sqlite3:onboarding_progress_outbox',
    'sql:canonical.sqlite3:account_messages',
    'sql:projections.sqlite3:projection_messages',
    'sql:analytics-projections.sqlite3:graph_nodes',
    'chrome-local:ofca_full_storage_bootstrap_v1',
    'chrome-session:active_account_partition_v5',
    'sqlite:auth.sqlite3:wal-shm',
    'sqlite:projections.sqlite3:wal-shm',
    'backup:projections:file',
    'backup:protected-key-sidecar',
    'restore:database:temporary',
    'key:.ofca-master-key.dpapi',
    'boundary:filesystem-deleted-blocks-journal-cow',
  ]) assert.ok(ids.has(expected), `source-surface discovery missed ${expected}`);
});

test('RET-001 COMPLETE source-surface audit has zero unexplained surfaces and passes fail-closed gate', async () => {
  const { registry } = await loadRet001Registry();
  const audit = await buildRet001SourceSurfaceAudit(registry);
  assert.equal(registry.inventory_status, 'COMPLETE');
  assert.equal(audit.inventory_status, 'COMPLETE');
  assert.ok(audit.surface_count > 0);
  assert.equal(audit.unmapped_surface_count, 0);
  assert.deepEqual(audit.unmapped_surface_ids, []);
  assert.equal(audit.completeness_gate.enforced, true);
  assert.equal(audit.completeness_gate.status, 'PASS');
  assert.ok(audit.disposition_counts.REGISTERED_OBJECT > 0);
  assert.ok(audit.disposition_counts.COVERED_BY_EXISTING_OBJECT > 0);
  assert.ok(audit.disposition_counts.OUTSIDE_RET001_SCOPE > 0);
  assert.ok(audit.disposition_counts.UNVERIFIED_BELOW_SUPPORTED_BOUNDARY > 0);
  assert.doesNotThrow(() => assertRet001SourceSurfaceCompleteness(audit));
});

test('RET-001 auth schema tables are explicitly covered by the complete auth registry object', async () => {
  const audit = await buildRet001SourceSurfaceAudit();
  const auth = audit.surfaces.filter((surface) => surface.id.startsWith('sql:auth.sqlite3:'));
  assert.ok(auth.length >= 15);
  for (const surface of auth) {
    assert.equal(surface.status, 'REGISTERED_OBJECT');
    assert.ok(surface.registry_object_ids.includes('COMP-AUTH-DATASET-STATE'));
  }
});

test('RET-001 source audit records explicit out-of-scope and lower-boundary unverified dispositions', async () => {
  const audit = await buildRet001SourceSurfaceAudit();
  for (const id of [
    'chrome-local:ofca_preview_metrics_v1',
    'chrome-legacy:brain_binding_v2',
    'migration:installation-lock',
  ]) assert.ok(audit.outside_scope_surface_ids.includes(id), `${id} must be out of RET-001 scope`);
  for (const id of [
    'boundary:browser-indexeddb-physical-remnants',
    'boundary:browser-chrome-storage-physical-remnants',
    'boundary:filesystem-deleted-blocks-journal-cow',
    'boundary:process-memory-pagefile-swap-crash-dumps',
    'boundary:windows-dpapi-forensic-internals',
  ]) assert.ok(audit.unverified_surface_ids.includes(id), `${id} must be explicitly unverified`);
});

test('RET-001 aggregate application mappings remain explicit', async () => {
  const audit = await buildRet001SourceSurfaceAudit();
  const byId = new Map(audit.surfaces.map((surface) => [surface.id, surface]));
  for (const [surfaceId, expectedObjectIds] of Object.entries(RET001_AGGREGATE_SURFACE_OBJECTS)) {
    const surface = byId.get(surfaceId);
    assert.ok(surface, `aggregate surface ${surfaceId} must exist`);
    for (const objectId of expectedObjectIds) assert.ok(surface.registry_object_ids.includes(objectId));
  }
});

test('RET-001 COMPLETE source audit still fails closed if a discovered registered object is removed', async () => {
  const { registry } = await loadRet001Registry();
  const mutated = structuredClone(registry);
  mutated.objects = mutated.objects.filter((entry) => entry.id !== 'EXT-ENCRYPTION-KEY-CHECK');
  const audit = await buildRet001SourceSurfaceAudit(mutated);
  assert.equal(audit.completeness_gate.status, 'FAIL');
  assert.ok(audit.unmapped_surface_ids.includes('idb:full-account:__ofca_encryption_key_check'));
  assert.throws(() => assertRet001SourceSurfaceCompleteness(audit), /source-surface completeness failed/);
});
