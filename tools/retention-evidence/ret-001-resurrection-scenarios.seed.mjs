import { validateRet001Registry } from './ret-001-registry.mjs';
import { buildRet001ReconstructionGraph } from './ret-001-views.mjs';

export const RET001_RESURRECTION_SCENARIO_CATALOG_SCHEMA =
  'ofca-ret-001-resurrection-scenario-catalog/v1';
export const RET001_RESURRECTION_SCENARIO_BINDINGS_SCHEMA =
  'ofca-ret-001-resurrection-scenario-bindings/v1';
export const RET001_RESURRECTION_SCENARIO_RESULT_SCHEMA =
  'ofca-ret-001-resurrection-scenario-result/v1';

const RESULT_STATUSES = Object.freeze([
  'OBSERVED_REENTRY',
  'NO_REENTRY_OBSERVED',
  'INCOMPLETE',
  'NOT_APPLICABLE',
]);
const OBSERVATION_STATUSES = Object.freeze([
  'OBSERVED',
  'NOT_OBSERVED',
  'INCONCLUSIVE',
]);
const SHA_PATTERN = /^[0-9a-f]{40}$/;

function freezeRequirement(id, from, relationship, to, purpose) {
  return Object.freeze({ id, from, relationship, to, purpose });
}

function freezeScenario({
  id,
  family,
  title,
  question,
  objectIds,
  deletedObjectIds,
  requirements,
  modelCoverage,
}) {
  return Object.freeze({
    id,
    family,
    title,
    question,
    execution_status: 'PLANNED_OBSERVATIONAL',
    production_lifecycle_change_authorized: false,
    object_ids: Object.freeze([...objectIds]),
    deleted_object_ids: Object.freeze([...deletedObjectIds]),
    relationship_requirements: Object.freeze(requirements),
    model_coverage: modelCoverage,
  });
}

export const RET001_RESURRECTION_SCENARIOS = Object.freeze([
  freezeScenario({
    id: 'RES-001-EXT-OUTBOX-RETRY',
    family: 'extension_retry_after_companion_deletion',
    title: 'Extension outbox retry after companion-side deletion',
    question:
      'Can surviving Extension delivery state reintroduce deleted companion message information?',
    objectIds: ['EXT-OUTBOX', 'COMP-RAW-INGEST-EVENTS', 'COMP-CANONICAL-MESSAGES'],
    deletedObjectIds: ['COMP-RAW-INGEST-EVENTS', 'COMP-CANONICAL-MESSAGES'],
    requirements: [
      freezeRequirement(
        'RES-001-R1',
        'EXT-OUTBOX',
        'copies_to',
        'COMP-RAW-INGEST-EVENTS',
        'Bind the durable Extension retry source to accepted raw ingest state.',
      ),
      freezeRequirement(
        'RES-001-R2',
        'EXT-OUTBOX',
        'rebuild_or_replay_source',
        'COMP-CANONICAL-MESSAGES',
        'Bind Extension queued delivery state to the declared canonical-message replay path.',
      ),
    ],
    modelCoverage: 'END_TO_END_RELATIONSHIP_PREREQUISITES_DECLARED',
  }),
  freezeScenario({
    id: 'RES-002-HISTORY-SNAPSHOT-REPLAY',
    family: 'history_snapshot_replay',
    title: 'History or snapshot replay after companion-side deletion',
    question:
      'Can surviving Extension snapshot material reintroduce deleted canonical message information?',
    objectIds: ['EXT-SNAPSHOT-CHUNKS', 'COMP-CANONICAL-MESSAGES'],
    deletedObjectIds: ['COMP-CANONICAL-MESSAGES'],
    requirements: [
      freezeRequirement(
        'RES-002-R1',
        'EXT-SNAPSHOT-CHUNKS',
        'copies_to',
        'COMP-CANONICAL-MESSAGES',
        'Bind snapshot material to the declared canonical-message copy path.',
      ),
    ],
    modelCoverage: 'DIRECT_RELATIONSHIP_PREREQUISITE_DECLARED',
  }),
  freezeScenario({
    id: 'RES-003-PROJECTION-REBUILD',
    family: 'projection_rebuild',
    title: 'Projection rebuild from surviving canonical messages',
    question:
      'Can surviving canonical message data recreate deleted Bridge projection message information?',
    objectIds: ['COMP-CANONICAL-MESSAGES', 'COMP-BRIDGE-PROJECTION-MESSAGES'],
    deletedObjectIds: ['COMP-BRIDGE-PROJECTION-MESSAGES'],
    requirements: [
      freezeRequirement(
        'RES-003-R1',
        'COMP-CANONICAL-MESSAGES',
        'rebuild_or_replay_source',
        'COMP-BRIDGE-PROJECTION-MESSAGES',
        'Bind the canonical source to the declared Bridge projection rebuild path.',
      ),
    ],
    modelCoverage: 'DIRECT_REBUILD_RELATIONSHIP_DECLARED',
  }),
  freezeScenario({
    id: 'RES-004-MIGRATION-BACKUP-RESTORE',
    family: 'migration_backup_restore',
    title: 'Migration backup restore after live canonical deletion',
    question:
      'Can a surviving pre-migration backup participate in restoring information deleted from the live canonical store?',
    objectIds: ['COMP-CANONICAL-MESSAGES', 'COMP-MIGRATION-BACKUP', 'COMP-RESTORE-TEMP'],
    deletedObjectIds: ['COMP-CANONICAL-MESSAGES'],
    requirements: [
      freezeRequirement(
        'RES-004-R1',
        'COMP-CANONICAL-MESSAGES',
        'rebuild_or_replay_source',
        'COMP-MIGRATION-BACKUP',
        'Bind canonical message state to the declared pre-migration backup creation path.',
      ),
      freezeRequirement(
        'RES-004-R2',
        'COMP-MIGRATION-BACKUP',
        'rebuild_or_replay_source',
        'COMP-RESTORE-TEMP',
        'Bind the migration backup to restore staging.',
      ),
    ],
    modelCoverage: 'RESTORE_STAGING_RELATIONSHIPS_DECLARED_FINAL_PUBLICATION_REQUIRES_SCENARIO_PROOF',
  }),
  freezeScenario({
    id: 'RES-005-ORDINARY-BACKUP-RESTORE',
    family: 'ordinary_backup_restore',
    title: 'Ordinary backup restore after live canonical deletion',
    question:
      'Can a surviving canonical backup restore message information deleted from the live canonical store?',
    objectIds: ['COMP-CANONICAL-MESSAGES', 'COMP-BACKUP-CANONICAL', 'COMP-RESTORE-TEMP'],
    deletedObjectIds: ['COMP-CANONICAL-MESSAGES'],
    requirements: [
      freezeRequirement(
        'RES-005-R1',
        'COMP-CANONICAL-MESSAGES',
        'rebuild_or_replay_source',
        'COMP-BACKUP-CANONICAL',
        'Bind canonical message state to the declared backup creation path.',
      ),
      freezeRequirement(
        'RES-005-R2',
        'COMP-BACKUP-CANONICAL',
        'rebuild_or_replay_source',
        'COMP-RESTORE-TEMP',
        'Bind the canonical backup to restore staging.',
      ),
    ],
    modelCoverage: 'RESTORE_STAGING_RELATIONSHIPS_DECLARED_FINAL_PUBLICATION_REQUIRES_SCENARIO_PROOF',
  }),
  freezeScenario({
    id: 'RES-006-STALE-EXTENSION-AFTER-COMPANION-DELETE',
    family: 'stale_extension_after_companion_deletion',
    title: 'Stale Extension state after companion-side deletion',
    question:
      'Can surviving Extension message state cause deleted companion canonical message information to exist again?',
    objectIds: ['EXT-MESSAGES', 'COMP-CANONICAL-MESSAGES'],
    deletedObjectIds: ['COMP-CANONICAL-MESSAGES'],
    requirements: [
      freezeRequirement(
        'RES-006-R1',
        'EXT-MESSAGES',
        'rebuild_or_replay_source',
        'COMP-CANONICAL-MESSAGES',
        'Bind surviving Extension message state to the declared canonical replay path.',
      ),
    ],
    modelCoverage: 'DIRECT_REPLAY_RELATIONSHIP_DECLARED',
  }),
  freezeScenario({
    id: 'RES-007-STALE-COMPANION-AFTER-EXTENSION-DELETE',
    family: 'stale_companion_after_extension_delete_all',
    title: 'Stale companion state after Extension delete-all',
    question:
      'What companion message information survives after the Extension-side source is deleted?',
    objectIds: ['EXT-MESSAGES', 'COMP-CANONICAL-MESSAGES'],
    deletedObjectIds: ['EXT-MESSAGES'],
    requirements: [
      freezeRequirement(
        'RES-007-R1',
        'EXT-MESSAGES',
        'copies_to',
        'COMP-CANONICAL-MESSAGES',
        'Bind the Extension source to the surviving companion canonical copy.',
      ),
    ],
    modelCoverage: 'SURVIVING_DOWNSTREAM_COPY_RELATIONSHIP_DECLARED',
  }),
  freezeScenario({
    id: 'RES-008-RECONNECT-AFTER-DISCONNECT',
    family: 'reconnect_after_account_disconnect',
    title: 'Reconnect or reconcile after account disconnect',
    question:
      'Can surviving queued Extension state be replayed into companion state after reconnect or reconciliation?',
    objectIds: ['EXT-OUTBOX', 'COMP-RAW-INGEST-EVENTS', 'COMP-CANONICAL-MESSAGES'],
    deletedObjectIds: ['COMP-CANONICAL-MESSAGES'],
    requirements: [
      freezeRequirement(
        'RES-008-R1',
        'EXT-OUTBOX',
        'copies_to',
        'COMP-RAW-INGEST-EVENTS',
        'Bind queued Extension delivery to companion ingest after reconnect.',
      ),
      freezeRequirement(
        'RES-008-R2',
        'COMP-RAW-INGEST-EVENTS',
        'copies_to',
        'COMP-CANONICAL-MESSAGES',
        'Bind accepted ingest evidence to canonical message reconstruction.',
      ),
    ],
    modelCoverage: 'END_TO_END_RELATIONSHIP_PREREQUISITES_DECLARED',
  }),
  freezeScenario({
    id: 'RES-009-DERIVED-STORE-REBUILD-AFTER-SOURCE-DELETE',
    family: 'derived_store_rebuild_after_source_deletion',
    title: 'Derived-store behavior after canonical source deletion',
    question:
      'After canonical source deletion, what derived message information survives and what can still be rebuilt?',
    objectIds: ['COMP-CANONICAL-MESSAGES', 'COMP-BRIDGE-PROJECTION-MESSAGES'],
    deletedObjectIds: ['COMP-CANONICAL-MESSAGES'],
    requirements: [
      freezeRequirement(
        'RES-009-R1',
        'COMP-CANONICAL-MESSAGES',
        'copies_to',
        'COMP-BRIDGE-PROJECTION-MESSAGES',
        'Bind canonical messages to the surviving derived projection copy.',
      ),
    ],
    modelCoverage: 'SURVIVING_DERIVED_COPY_RELATIONSHIP_DECLARED',
  }),
]);

function edgeKey(from, relationship, to) {
  return `${from}|${relationship}|${to}`;
}

function stableUnique(values) {
  return [...new Set(values)].sort();
}

export function buildRet001ResurrectionScenarioCatalog(registry) {
  validateRet001Registry(registry);
  const objectIds = new Set(registry.objects.map((entry) => entry.id));
  for (const scenario of RET001_RESURRECTION_SCENARIOS) {
    for (const objectId of scenario.object_ids) {
      if (!objectIds.has(objectId)) {
        throw new Error(`RET-001 scenario ${scenario.id} references unknown object ${objectId}`);
      }
    }
    for (const objectId of scenario.deleted_object_ids) {
      if (!objectIds.has(objectId)) {
        throw new Error(`RET-001 scenario ${scenario.id} deletes unknown object ${objectId}`);
      }
    }
  }
  return Object.freeze({
    schema: RET001_RESURRECTION_SCENARIO_CATALOG_SCHEMA,
    product_baseline_revision: registry.product_baseline_revision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    execution_policy: Object.freeze({
      observational_only: true,
      production_lifecycle_change_authorized: false,
      retention_duration_selected: false,
    }),
    scenarios: RET001_RESURRECTION_SCENARIOS,
  });
}

export function buildRet001ResurrectionScenarioBindings(registry) {
  const catalog = buildRet001ResurrectionScenarioCatalog(registry);
  const graph = buildRet001ReconstructionGraph(registry);
  const edges = new Map(
    graph.edges.map((edge) => [edgeKey(edge.from, edge.relationship, edge.to), edge]),
  );

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
        evidence_granularity: edge === null
          ? 'NONE'
          : 'OBJECT_DECLARATION_LEVEL_PENDING_SCENARIO_EXECUTION',
      });
    });
    const missing = requirements.filter(
      (requirement) => requirement.binding_status !== 'BOUND_TO_REGISTRY_EDGE',
    );
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

  const missingRequirementIds = scenarios.flatMap(
    (scenario) => scenario.missing_relationship_requirement_ids,
  );
  return Object.freeze({
    schema: RET001_RESURRECTION_SCENARIO_BINDINGS_SCHEMA,
    product_baseline_revision: registry.product_baseline_revision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    binding_gate: Object.freeze({
      enforced: registry.inventory_status === 'COMPLETE',
      status: registry.inventory_status === 'COMPLETE'
        ? (missingRequirementIds.length === 0 ? 'PASS' : 'FAIL')
        : 'DEFERRED_UNTIL_INVENTORY_COMPLETE',
    }),
    summary: Object.freeze({
      scenario_count: scenarios.length,
      relationship_requirement_count: scenarios.reduce(
        (total, scenario) => total + scenario.relationship_requirements.length,
        0,
      ),
      missing_relationship_requirement_count: missingRequirementIds.length,
    }),
    missing_relationship_requirement_ids: Object.freeze(stableUnique(missingRequirementIds)),
    scenarios: Object.freeze(scenarios),
  });
}

export function assertRet001ScenarioBindingCompleteness(registry) {
  const bindings = buildRet001ResurrectionScenarioBindings(registry);
  if (!bindings.binding_gate.enforced) return bindings;
  if (bindings.binding_gate.status !== 'PASS') {
    throw new Error(
      `RET-001 resurrection scenario binding failed: ${bindings.missing_relationship_requirement_ids.join(', ')}`,
    );
  }
  return bindings;
}

export function validateRet001ResurrectionScenarioResult(registry, result) {
  validateRet001Registry(registry);
  const scenario = RET001_RESURRECTION_SCENARIOS.find(
    (candidate) => candidate.id === result?.scenario_id,
  );
  if (result?.schema !== RET001_RESURRECTION_SCENARIO_RESULT_SCHEMA) {
    throw new Error('RET-001 resurrection scenario result schema differs');
  }
  if (!scenario) {
    throw new Error(`RET-001 resurrection scenario result references unknown scenario ${result?.scenario_id}`);
  }
  if (!SHA_PATTERN.test(result.product_revision ?? '')) {
    throw new Error('RET-001 resurrection scenario result product_revision must be lowercase 40-hex');
  }
  if (!RESULT_STATUSES.includes(result.result_status)) {
    throw new Error(`RET-001 resurrection scenario result status is invalid: ${result.result_status}`);
  }
  if (Number.isNaN(Date.parse(result.executed_at ?? ''))) {
    throw new Error('RET-001 resurrection scenario result executed_at must be an ISO timestamp');
  }
  if (!Array.isArray(result.observations) || result.observations.length === 0) {
    throw new Error('RET-001 resurrection scenario result observations must be non-empty');
  }

  const requirementIds = new Set(
    scenario.relationship_requirements.map((requirement) => requirement.id),
  );
  const observedRequirementIds = new Set();
  for (const observation of result.observations) {
    if (!requirementIds.has(observation.relationship_requirement_id)) {
      throw new Error(
        `RET-001 resurrection scenario result references unknown relationship requirement ${observation.relationship_requirement_id}`,
      );
    }
    if (observedRequirementIds.has(observation.relationship_requirement_id)) {
      throw new Error(
        `RET-001 resurrection scenario result duplicates relationship requirement ${observation.relationship_requirement_id}`,
      );
    }
    observedRequirementIds.add(observation.relationship_requirement_id);
    if (!OBSERVATION_STATUSES.includes(observation.outcome)) {
      throw new Error(
        `RET-001 resurrection scenario observation outcome is invalid: ${observation.outcome}`,
      );
    }
    if (!Array.isArray(observation.evidence_ids) || observation.evidence_ids.length === 0) {
      throw new Error('RET-001 resurrection scenario observation evidence_ids must be non-empty');
    }
  }

  if (result.result_status !== 'INCOMPLETE') {
    for (const requirementId of requirementIds) {
      if (!observedRequirementIds.has(requirementId)) {
        throw new Error(
          `RET-001 completed resurrection scenario result is missing requirement observation ${requirementId}`,
        );
      }
    }
  }
  return result;
}
