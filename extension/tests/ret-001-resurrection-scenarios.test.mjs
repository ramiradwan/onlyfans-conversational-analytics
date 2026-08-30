import assert from 'node:assert/strict';
import test from 'node:test';

import { loadRet001Registry } from '../../tools/retention-evidence/ret-001-registry.mjs';
import {
  RET001_RESURRECTION_SCENARIO_RESULT_SCHEMA,
  RET001_RESURRECTION_SCENARIOS,
  assertRet001ScenarioBindingCompleteness,
  buildRet001ResurrectionScenarioBindings,
  buildRet001ResurrectionScenarioCatalog,
  validateRet001ResurrectionScenarioResult,
} from '../../tools/retention-evidence/ret-001-resurrection-scenarios.mjs';

const REQUIRED_FAMILIES = Object.freeze([
  'extension_retry_after_companion_deletion','history_snapshot_replay','projection_rebuild',
  'migration_backup_restore','ordinary_backup_restore','stale_extension_after_companion_deletion',
  'stale_companion_after_extension_delete_all','reconnect_after_account_disconnect',
  'derived_store_rebuild_after_source_deletion','projection_backup_restore',
]);

test('RET-001 resurrection scenario catalog covers the complete closure families', async () => {
  const { registry } = await loadRet001Registry();
  const catalog = buildRet001ResurrectionScenarioCatalog(registry);
  assert.equal(catalog.execution_policy.observational_only, true);
  assert.equal(catalog.execution_policy.production_lifecycle_change_authorized, false);
  assert.equal(catalog.execution_policy.retention_duration_selected, false);
  assert.deepEqual(catalog.scenarios.map((scenario) => scenario.family).sort(), [...REQUIRED_FAMILIES].sort());
  assert.equal(catalog.scenarios.length, 10);
  for (const scenario of catalog.scenarios) {
    assert.match(scenario.id, /^RES-\d{3}-[A-Z0-9-]+$/);
    assert.equal(scenario.execution_status, 'PLANNED_OBSERVATIONAL');
    assert.ok(scenario.object_ids.length > 0);
    assert.ok(scenario.deleted_object_ids.length > 0);
    assert.ok(scenario.relationship_requirements.length > 0);
  }
});

test('RET-001 COMPLETE scenario bindings pass with the projection-backup path bound', async () => {
  const { registry } = await loadRet001Registry();
  const bindings = buildRet001ResurrectionScenarioBindings(registry);
  assert.equal(bindings.summary.scenario_count, 10);
  assert.equal(bindings.inventory_status, 'COMPLETE');
  assert.equal(bindings.binding_gate.enforced, true);
  assert.equal(bindings.binding_gate.status, 'PASS');
  assert.deepEqual(bindings.missing_relationship_requirement_ids, []);
  const projectionBackup = bindings.scenarios.find((scenario) => scenario.scenario_id === 'RES-010-PROJECTION-BACKUP-RESTORE');
  assert.ok(projectionBackup);
  assert.equal(projectionBackup.binding_status, 'BOUND');
  assert.equal(projectionBackup.relationship_requirements.length, 3);
  for (const requirement of projectionBackup.relationship_requirements) {
    assert.equal(requirement.binding_status, 'BOUND_TO_REGISTRY_EDGE');
    assert.ok(requirement.evidence_ids.length > 0);
    assert.ok(requirement.source_references.length > 0);
  }
  assert.doesNotThrow(() => assertRet001ScenarioBindingCompleteness(registry));
});

test('RET-001 scenario binding gate fails closed when a required projection-backup edge is removed', async () => {
  const { registry } = await loadRet001Registry();
  const mutated = structuredClone(registry);
  const backup = mutated.objects.find((entry) => entry.id === 'COMP-BACKUP-PROJECTIONS');
  backup.reconstruction.copies_to_object_ids = [];
  const restore = mutated.objects.find((entry) => entry.id === 'COMP-RESTORE-TEMP');
  restore.reconstruction.rebuild_or_replay_source_object_ids = restore.reconstruction.rebuild_or_replay_source_object_ids.filter((id) => id !== 'COMP-BACKUP-PROJECTIONS');
  assert.throws(() => assertRet001ScenarioBindingCompleteness(mutated), /RES-010-R2/);
});

test('RET-001 projection-backup scenario result binds all three executed requirements', async () => {
  const { registry } = await loadRet001Registry();
  const result = {
    schema: RET001_RESURRECTION_SCENARIO_RESULT_SCHEMA,
    scenario_id: 'RES-010-PROJECTION-BACKUP-RESTORE',
    product_revision: '1'.repeat(40),
    executed_at: '2026-08-30T16:00:00.000Z',
    result_status: 'OBSERVED_REENTRY',
    observations: ['RES-010-R1','RES-010-R2','RES-010-R3'].map((id) => ({ relationship_requirement_id: id, outcome: 'OBSERVED', evidence_ids: [`EXEC-${id}`] })),
  };
  assert.equal(validateRet001ResurrectionScenarioResult(registry, result), result);
});
