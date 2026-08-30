import assert from 'node:assert/strict';
import test from 'node:test';

import { loadRet001Registry } from '../../tools/retention-evidence/ret-001-registry.mjs';
import { buildRet001ExecutedScenarioSummary } from '../../tools/retention-evidence/ret-001-executed-scenarios.mjs';
import { RET001_RESURRECTION_SCENARIO_RESULT_SCHEMA, RET001_RESURRECTION_SCENARIOS } from '../../tools/retention-evidence/ret-001-resurrection-scenarios.mjs';

const REVISION = '1'.repeat(40);
function result(scenarioId, requirementIds) {
  return {
    schema: RET001_RESURRECTION_SCENARIO_RESULT_SCHEMA,
    scenario_id: scenarioId,
    product_revision: REVISION,
    executed_at: '2026-08-30T12:00:00Z',
    result_status: 'OBSERVED_REENTRY',
    observations: requirementIds.map((id) => ({
      relationship_requirement_id: id,
      outcome: 'OBSERVED',
      evidence_ids: [`EXEC-${id}`],
    })),
  };
}
function file(document) {
  return { name: `${document.scenario_id.toLowerCase()}.json`, text: `${JSON.stringify(document)}\n`, result: document };
}

test('RET-001 executed scenario summary follows the 10-scenario closure catalog', async () => {
  const { registry } = await loadRet001Registry();
  const files = [
    file(result('RES-003-PROJECTION-REBUILD', ['RES-003-R1'])),
    file(result('RES-005-ORDINARY-BACKUP-RESTORE', ['RES-005-R1', 'RES-005-R2'])),
    file(result('RES-010-PROJECTION-BACKUP-RESTORE', ['RES-010-R1', 'RES-010-R2', 'RES-010-R3'])),
  ];
  const summary = buildRet001ExecutedScenarioSummary(registry, files, REVISION);
  assert.equal(RET001_RESURRECTION_SCENARIOS.length, 10);
  assert.equal(summary.execution_coverage_status, 'PARTIAL');
  assert.equal(summary.summary.executed_scenario_count, 3);
  assert.equal(summary.summary.pending_scenario_count, 7);
  assert.ok(summary.executed_scenario_ids.includes('RES-010-PROJECTION-BACKUP-RESTORE'));
});

test('RET-001 executed scenario summary rejects mixed execution revisions', async () => {
  const { registry } = await loadRet001Registry();
  const document = result('RES-010-PROJECTION-BACKUP-RESTORE', ['RES-010-R1', 'RES-010-R2', 'RES-010-R3']);
  document.product_revision = '2'.repeat(40);
  assert.throws(
    () => buildRet001ExecutedScenarioSummary(registry, [file(document)], REVISION),
    /revision differs from execution revision/,
  );
});
