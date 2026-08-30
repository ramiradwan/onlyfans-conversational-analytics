import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { scenarioById } from '../../tools/legal-evidence/activation-scenarios.mjs';

const REVISION = process.env.OFCA_PRODUCT_REVISION ?? null;
const OUTPUT_ROOT = process.env.OFCA_LEGAL_EVIDENCE_EXECUTION_ROOT ?? null;

function json(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

export async function emitLegalScenarioEvidence(id, evidence) {
  if (OUTPUT_ROOT === null) return;
  if (!/^[a-f0-9]{40}$/.test(REVISION ?? '')) {
    throw new Error('OFCA_PRODUCT_REVISION must bind emitted Legal evidence to an exact commit');
  }
  const scenario = scenarioById(id);
  if (scenario === null) throw new Error(`Unknown Legal evidence scenario ${id}`);
  if (scenario.evidence_files.length !== 1) {
    throw new Error(`${id} must declare exactly one primary evidence file`);
  }

  const root = path.resolve(OUTPUT_ROOT);
  const resultPath = path.join(root, scenario.result_file);
  const evidencePath = path.join(root, scenario.evidence_files[0]);
  await Promise.all([
    mkdir(path.dirname(resultPath), { recursive: true }),
    mkdir(path.dirname(evidencePath), { recursive: true }),
  ]);
  await writeFile(evidencePath, json({
    schema: 'ofca-product-legal-scenario-evidence/v1',
    product_revision: REVISION,
    scenario_id: id,
    synthetic_instrument_bindings: true,
    evidence,
  }), 'utf8');
  await writeFile(resultPath, json({
    schema: 'ofca-product-legal-scenario-result/v1',
    product_revision: REVISION,
    scenario_id: id,
    product_test_status: 'PASS',
    legal_acceptance_status: 'UNSCORED',
    executing_test: scenario.test_name,
    evidence_files: scenario.evidence_files,
  }), 'utf8');
}
