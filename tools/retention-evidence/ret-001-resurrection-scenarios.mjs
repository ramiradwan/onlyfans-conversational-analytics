import { validateRet001Registry } from './ret-001-registry.mjs';
import { buildRet001ReconstructionGraph } from './ret-001-views.mjs';
import {
  RET001_RESURRECTION_SCENARIOS as SEED_SCENARIOS,
  RET001_RESURRECTION_SCENARIO_CATALOG_SCHEMA,
  RET001_RESURRECTION_SCENARIO_BINDINGS_SCHEMA,
  RET001_RESURRECTION_SCENARIO_RESULT_SCHEMA,
} from './ret-001-resurrection-scenarios.seed.mjs';

export {
  RET001_RESURRECTION_SCENARIO_CATALOG_SCHEMA,
  RET001_RESURRECTION_SCENARIO_BINDINGS_SCHEMA,
  RET001_RESURRECTION_SCENARIO_RESULT_SCHEMA,
};

const RES010 = Object.freeze({
  id: 'RES-010-PROJECTION-BACKUP-RESTORE',
  family: 'projection_backup_restore',
  title: 'Analytics projection backup restore after live derived-store deletion',
  question: 'Can a surviving analytics-projections.sqlite3 backup restore linkable analytics information deleted from the live analytics projection store?',
  execution_status: 'PLANNED_OBSERVATIONAL',
  production_lifecycle_change_authorized: false,
  object_ids: Object.freeze(['COMP-ANALYTICS-PROJECTION', 'COMP-BACKUP-PROJECTIONS', 'COMP-RESTORE-TEMP']),
  deleted_object_ids: Object.freeze(['COMP-ANALYTICS-PROJECTION']),
  relationship_requirements: Object.freeze([
    Object.freeze({
      id: 'RES-010-R1',
      from: 'COMP-ANALYTICS-PROJECTION',
      relationship: 'rebuild_or_replay_source',
      to: 'COMP-BACKUP-PROJECTIONS',
      purpose: 'Bind the persisted analytics projection to the production projection-backup creation path.',
    }),
    Object.freeze({
      id: 'RES-010-R2',
      from: 'COMP-BACKUP-PROJECTIONS',
      relationship: 'rebuild_or_replay_source',
      to: 'COMP-RESTORE-TEMP',
      purpose: 'Bind the verified analytics projection backup to restore staging.',
    }),
    Object.freeze({
      id: 'RES-010-R3',
      from: 'COMP-RESTORE-TEMP',
      relationship: 'copies_to',
      to: 'COMP-ANALYTICS-PROJECTION',
      purpose: 'Bind verified restore staging to republished analytics projection state.',
    }),
  ]),
  model_coverage: 'ANALYTICS_PROJECTION_BACKUP_RESTORE_PATH_DECLARED_AND_EXECUTION_REQUIRED',
});

export const RET001_RESURRECTION_SCENARIOS = Object.freeze([...SEED_SCENARIOS, RES010]);
const RESULT_STATUSES = Object.freeze(['OBSERVED_REENTRY', 'NO_REENTRY_OBSERVED', 'INCOMPLETE', 'NOT_APPLICABLE']);
const OBSERVATION_STATUSES = Object.freeze(['OBSERVED', 'NOT_OBSERVED', 'INCONCLUSIVE']);
const SHA_PATTERN = /^[0-9a-f]{40}$/;
const edgeKey = (from, relationship, to) => `${from}|${relationship}|${to}`;
const stableUnique = (values) => [...new Set(values)].sort();

export function buildRet001ResurrectionScenarioCatalog(registry) {
  validateRet001Registry(registry);
  const objectIds = new Set(registry.objects.map((entry) => entry.id));
  for (const scenario of RET001_RESURRECTION_SCENARIOS) {
    for (const objectId of [...scenario.object_ids, ...scenario.deleted_object_ids]) {
      if (!objectIds.has(objectId)) throw new Error(`RET-001 scenario ${scenario.id} references unknown object ${objectId}`);
    }
  }
  return Object.freeze({
    schema: RET001_RESURRECTION_SCENARIO_CATALOG_SCHEMA,
    product_baseline_revision: registry.product_baseline_revision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    execution_policy: Object.freeze({ observational_only: true, production_lifecycle_change_authorized: false, retention_duration_selected: false }),
    scenarios: RET001_RESURRECTION_SCENARIOS,
  });
}

export function buildRet001ResurrectionScenarioBindings(registry) {
  const catalog = buildRet001ResurrectionScenarioCatalog(registry);
  const graph = buildRet001ReconstructionGraph(registry);
  const edges = new Map(graph.edges.map((edge) => [edgeKey(edge.from, edge.relationship, edge.to), edge]));
  const scenarios = catalog.scenarios.map((scenario) => {
    const requirements = scenario.relationship_requirements.map((requirement) => {
      const key = edgeKey(requirement.from, requirement.relationship, requirement.to);
      const edge = edges.get(key) ?? null;
      return Object.freeze({
        ...requirement,
        edge_key: key,
        binding_status: edge === null ? 'MISSING_REGISTRY_RELATIONSHIP' : 'BOUND_TO_REGISTRY_EDGE',
        declared_by_object_id: edge?.declared_by_object_id ?? null,
        evidence_ids: Object.freeze(edge === null ? [] : [...edge.evidence_ids]),
        source_references: Object.freeze(edge === null ? [] : [...edge.source_references]),
        evidence_granularity: edge === null ? 'NONE' : 'OBJECT_DECLARATION_LEVEL_PENDING_SCENARIO_EXECUTION',
      });
    });
    const missing = requirements.filter((item) => item.binding_status !== 'BOUND_TO_REGISTRY_EDGE');
    return Object.freeze({
      scenario_id: scenario.id,
      family: scenario.family,
      execution_status: scenario.execution_status,
      model_coverage: scenario.model_coverage,
      binding_status: missing.length === 0 ? 'BOUND' : 'PARTIALLY_BOUND',
      missing_relationship_requirement_ids: Object.freeze(missing.map((item) => item.id)),
      relationship_requirements: Object.freeze(requirements),
    });
  });
  const missingRequirementIds = scenarios.flatMap((scenario) => scenario.missing_relationship_requirement_ids);
  return Object.freeze({
    schema: RET001_RESURRECTION_SCENARIO_BINDINGS_SCHEMA,
    product_baseline_revision: registry.product_baseline_revision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    binding_gate: Object.freeze({ enforced: registry.inventory_status === 'COMPLETE', status: registry.inventory_status === 'COMPLETE' ? (missingRequirementIds.length === 0 ? 'PASS' : 'FAIL') : 'DEFERRED_UNTIL_INVENTORY_COMPLETE' }),
    summary: Object.freeze({ scenario_count: scenarios.length, relationship_requirement_count: scenarios.reduce((total, scenario) => total + scenario.relationship_requirements.length, 0), missing_relationship_requirement_count: missingRequirementIds.length }),
    missing_relationship_requirement_ids: Object.freeze(stableUnique(missingRequirementIds)),
    scenarios: Object.freeze(scenarios),
  });
}

export function assertRet001ScenarioBindingCompleteness(registry) {
  const bindings = buildRet001ResurrectionScenarioBindings(registry);
  if (bindings.binding_gate.enforced && bindings.binding_gate.status !== 'PASS') {
    throw new Error(`RET-001 resurrection scenario binding failed: ${bindings.missing_relationship_requirement_ids.join(', ')}`);
  }
  return bindings;
}

export function validateRet001ResurrectionScenarioResult(registry, result) {
  validateRet001Registry(registry);
  const scenario = RET001_RESURRECTION_SCENARIOS.find((candidate) => candidate.id === result?.scenario_id);
  if (result?.schema !== RET001_RESURRECTION_SCENARIO_RESULT_SCHEMA) throw new Error('RET-001 resurrection scenario result schema differs');
  if (!scenario) throw new Error(`RET-001 resurrection scenario result references unknown scenario ${result?.scenario_id}`);
  if (!SHA_PATTERN.test(result.product_revision ?? '')) throw new Error('RET-001 resurrection scenario result product_revision must be lowercase 40-hex');
  if (!RESULT_STATUSES.includes(result.result_status)) throw new Error(`RET-001 resurrection scenario result status is invalid: ${result.result_status}`);
  if (Number.isNaN(Date.parse(result.executed_at ?? ''))) throw new Error('RET-001 resurrection scenario result executed_at must be an ISO timestamp');
  if (!Array.isArray(result.observations) || result.observations.length === 0) throw new Error('RET-001 resurrection scenario result observations must be non-empty');
  const requirementIds = new Set(scenario.relationship_requirements.map((requirement) => requirement.id));
  const observed = new Set();
  for (const observation of result.observations) {
    if (!requirementIds.has(observation.relationship_requirement_id)) throw new Error(`RET-001 resurrection scenario result references unknown relationship requirement ${observation.relationship_requirement_id}`);
    if (observed.has(observation.relationship_requirement_id)) throw new Error(`RET-001 resurrection scenario result duplicates relationship requirement ${observation.relationship_requirement_id}`);
    observed.add(observation.relationship_requirement_id);
    if (!OBSERVATION_STATUSES.includes(observation.outcome)) throw new Error(`RET-001 resurrection scenario observation outcome is invalid: ${observation.outcome}`);
    if (!Array.isArray(observation.evidence_ids) || observation.evidence_ids.length === 0) throw new Error('RET-001 resurrection scenario observation evidence_ids must be non-empty');
  }
  if (result.result_status !== 'INCOMPLETE') {
    for (const id of requirementIds) if (!observed.has(id)) throw new Error(`RET-001 completed resurrection scenario result is missing requirement observation ${id}`);
  }
  return result;
}
