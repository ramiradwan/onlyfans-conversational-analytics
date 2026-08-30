import { readdir, readFile } from 'node:fs/promises';
import { basename, resolve } from 'node:path';

import { validateRet001Registry } from './ret-001-registry.mjs';
import {
  RET001_RESURRECTION_SCENARIOS,
  buildRet001ResurrectionScenarioBindings,
  validateRet001ResurrectionScenarioResult,
} from './ret-001-resurrection-scenarios.mjs';

export const RET001_EXECUTED_SCENARIO_SUMMARY_SCHEMA =
  'ofca-ret-001-executed-scenario-summary/v1';

const SHA_PATTERN = /^[0-9a-f]{40}$/;

function stableUnique(values) {
  return [...new Set(values)].sort();
}

function promotionFor(outcome) {
  if (outcome === 'OBSERVED') return 'EXECUTED_OBSERVATION_AVAILABLE';
  if (outcome === 'NOT_OBSERVED') return 'EXECUTED_NON_OBSERVATION_AVAILABLE';
  return 'EXECUTED_INCONCLUSIVE_OBSERVATION';
}

export async function loadRet001ExecutedScenarioResults(
  registry,
  directory,
  productRevision,
) {
  validateRet001Registry(registry);
  if (!SHA_PATTERN.test(productRevision ?? '')) {
    throw new Error('RET-001 executed scenario product revision must be lowercase 40-hex');
  }
  const root = resolve(directory);
  const names = (await readdir(root))
    .filter((name) => name.endsWith('.json'))
    .sort();
  if (names.length === 0) {
    throw new Error('RET-001 executed scenario result directory contains no JSON results');
  }

  const seen = new Set();
  const files = [];
  for (const name of names) {
    const path = resolve(root, name);
    const text = await readFile(path, 'utf8');
    const result = JSON.parse(text);
    validateRet001ResurrectionScenarioResult(registry, result);
    if (result.product_revision !== productRevision) {
      throw new Error(
        `RET-001 scenario ${result.scenario_id} revision ${result.product_revision} differs from execution revision ${productRevision}`,
      );
    }
    if (seen.has(result.scenario_id)) {
      throw new Error(`RET-001 duplicate executed scenario result ${result.scenario_id}`);
    }
    seen.add(result.scenario_id);
    files.push(Object.freeze({
      name: basename(name),
      text,
      result: Object.freeze(result),
    }));
  }
  return Object.freeze(files);
}

export function buildRet001ExecutedScenarioSummary(
  registry,
  resultFiles,
  productRevision,
) {
  validateRet001Registry(registry);
  if (!SHA_PATTERN.test(productRevision ?? '')) {
    throw new Error('RET-001 executed scenario product revision must be lowercase 40-hex');
  }
  const bindings = buildRet001ResurrectionScenarioBindings(registry);
  const bindingMap = new Map(
    bindings.scenarios.map((scenario) => [scenario.scenario_id, scenario]),
  );
  const resultMap = new Map();
  for (const file of resultFiles) {
    validateRet001ResurrectionScenarioResult(registry, file.result);
    if (file.result.product_revision !== productRevision) {
      throw new Error(
        `RET-001 scenario ${file.result.scenario_id} revision differs from execution revision`,
      );
    }
    if (resultMap.has(file.result.scenario_id)) {
      throw new Error(`RET-001 duplicate executed scenario result ${file.result.scenario_id}`);
    }
    resultMap.set(file.result.scenario_id, file.result);
  }

  let observedRequirementCount = 0;
  let notObservedRequirementCount = 0;
  let inconclusiveRequirementCount = 0;
  const scenarios = RET001_RESURRECTION_SCENARIOS.map((scenario) => {
    const result = resultMap.get(scenario.id) ?? null;
    const binding = bindingMap.get(scenario.id);
    const observations = new Map(
      (result?.observations ?? []).map((item) => [item.relationship_requirement_id, item]),
    );
    const requirements = scenario.relationship_requirements.map((requirement) => {
      const declaration = binding.relationship_requirements.find(
        (item) => item.id === requirement.id,
      );
      const observation = observations.get(requirement.id) ?? null;
      if (observation?.outcome === 'OBSERVED') observedRequirementCount += 1;
      if (observation?.outcome === 'NOT_OBSERVED') notObservedRequirementCount += 1;
      if (observation?.outcome === 'INCONCLUSIVE') inconclusiveRequirementCount += 1;
      return Object.freeze({
        relationship_requirement_id: requirement.id,
        edge_key: declaration?.edge_key ?? null,
        registry_binding_status: declaration?.binding_status ?? 'MISSING_REGISTRY_RELATIONSHIP',
        registry_evidence_ids: Object.freeze([...(declaration?.evidence_ids ?? [])]),
        execution_status: observation === null ? 'PENDING_EXECUTION' : 'EXECUTED',
        observation_outcome: observation?.outcome ?? null,
        execution_evidence_ids: Object.freeze([...(observation?.evidence_ids ?? [])]),
        evidence_promotion: observation === null
          ? 'PENDING_EXECUTION'
          : promotionFor(observation.outcome),
      });
    });
    return Object.freeze({
      scenario_id: scenario.id,
      family: scenario.family,
      execution_status: result === null ? 'PENDING_EXECUTION' : 'EXECUTED',
      result_status: result?.result_status ?? null,
      executed_at: result?.executed_at ?? null,
      relationship_requirements: Object.freeze(requirements),
    });
  });

  const executedIds = stableUnique([...resultMap.keys()]);
  const pendingIds = scenarios
    .filter((scenario) => scenario.execution_status === 'PENDING_EXECUTION')
    .map((scenario) => scenario.scenario_id);
  return Object.freeze({
    schema: RET001_EXECUTED_SCENARIO_SUMMARY_SCHEMA,
    product_baseline_revision: registry.product_baseline_revision,
    product_execution_revision: productRevision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    execution_coverage_status: pendingIds.length === 0 ? 'COMPLETE' : 'PARTIAL',
    summary: Object.freeze({
      catalog_scenario_count: RET001_RESURRECTION_SCENARIOS.length,
      executed_scenario_count: executedIds.length,
      pending_scenario_count: pendingIds.length,
      observed_relationship_requirement_count: observedRequirementCount,
      not_observed_relationship_requirement_count: notObservedRequirementCount,
      inconclusive_relationship_requirement_count: inconclusiveRequirementCount,
    }),
    executed_scenario_ids: Object.freeze(executedIds),
    pending_scenario_ids: Object.freeze(pendingIds),
    interpretation: Object.freeze({
      evidence_promotion:
        'Executed observations promote only the corresponding stable scenario requirement; they do not change Legal policy or imply a prevention architecture.',
      partial_coverage:
        'PARTIAL means at least one catalog scenario has no executed result for this exact Product revision.',
    }),
    scenarios: Object.freeze(scenarios),
  });
}
