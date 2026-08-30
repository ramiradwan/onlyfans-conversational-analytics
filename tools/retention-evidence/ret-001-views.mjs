import { validateRet001Registry } from './ret-001-registry.mjs';

export const RET001_MATRIX_SCHEMA = 'ofca-ret-001-object-matrix/v1';
export const RET001_GRAPH_SCHEMA = 'ofca-ret-001-reconstruction-graph/v1';
export const RET001_RESURRECTION_INDEX_SCHEMA = 'ofca-ret-001-resurrection-index/v1';

const RELATION_FIELDS = Object.freeze([
  Object.freeze({
    registryField: 'authoritative_source_object_ids',
    outputField: 'authoritative_source',
    relationship: 'authoritative_source',
    direction: 'incoming',
  }),
  Object.freeze({
    registryField: 'derived_from_object_ids',
    outputField: 'derived_from',
    relationship: 'derived_from',
    direction: 'incoming',
  }),
  Object.freeze({
    registryField: 'copies_to_object_ids',
    outputField: 'copies_to',
    relationship: 'copies_to',
    direction: 'outgoing',
  }),
  Object.freeze({
    registryField: 'rebuild_or_replay_source_object_ids',
    outputField: 'rebuild_or_replay_source',
    relationship: 'rebuild_or_replay_source',
    direction: 'incoming',
  }),
]);

function stableUnique(values) {
  return [...new Set(values)].sort();
}

function reconstructionView(entry) {
  return Object.freeze({
    authoritative_source: stableUnique(entry.reconstruction.authoritative_source_object_ids),
    derived_from: stableUnique(entry.reconstruction.derived_from_object_ids),
    copies_to: stableUnique(entry.reconstruction.copies_to_object_ids),
    rebuild_or_replay_source: stableUnique(
      entry.reconstruction.rebuild_or_replay_source_object_ids,
    ),
    can_recreate_content_or_equivalent_identifying_semantic_information:
      entry.reconstruction.can_recreate_deleted_content_or_identifying_semantics,
  });
}

function objectMap(registry) {
  return new Map(registry.objects.map((entry) => [entry.id, entry]));
}

function compareEdges(left, right) {
  return left.from.localeCompare(right.from)
    || left.to.localeCompare(right.to)
    || left.relationship.localeCompare(right.relationship)
    || left.declared_by_object_id.localeCompare(right.declared_by_object_id);
}

export function buildRet001ReconstructionGraph(registry) {
  validateRet001Registry(registry);
  const edges = [];
  for (const entry of registry.objects) {
    for (const relation of RELATION_FIELDS) {
      for (const relatedId of entry.reconstruction[relation.registryField]) {
        const from = relation.direction === 'incoming' ? relatedId : entry.id;
        const to = relation.direction === 'incoming' ? entry.id : relatedId;
        edges.push(Object.freeze({
          from,
          to,
          relationship: relation.relationship,
          declared_by_object_id: entry.id,
          evidence_ids: stableUnique(entry.evidence_ids),
          source_references: stableUnique(entry.source_references),
        }));
      }
    }
  }
  edges.sort(compareEdges);

  const nodes = registry.objects.map((entry) => Object.freeze({
    id: entry.id,
    system: entry.system,
    location: Object.freeze({ ...entry.location }),
    contains_raw_message_text: entry.raw_text_requirement.contains_raw_message_text,
    recreation_capability:
      entry.reconstruction.can_recreate_deleted_content_or_identifying_semantics,
    evidence_ids: stableUnique(entry.evidence_ids),
  }));

  return Object.freeze({
    schema: RET001_GRAPH_SCHEMA,
    product_baseline_revision: registry.product_baseline_revision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    relationship_semantics: Object.freeze({
      authoritative_source: 'source object declared as authoritative input for the target object',
      derived_from: 'target object is derived from the source object',
      copies_to: 'source object is declared to copy material into the target object',
      rebuild_or_replay_source: 'source object can feed a rebuild or replay path into the target object',
    }),
    nodes: Object.freeze(nodes),
    edges: Object.freeze(edges),
  });
}

function incomingEdges(graph) {
  const result = new Map();
  for (const node of graph.nodes) result.set(node.id, []);
  for (const edge of graph.edges) result.get(edge.to).push(edge);
  return result;
}

function outgoingEdges(graph) {
  const result = new Map();
  for (const node of graph.nodes) result.set(node.id, []);
  for (const edge of graph.edges) result.get(edge.from).push(edge);
  return result;
}

function recreationStatus(value) {
  if (value === true) return 'CONFIRMED_RECREATION_OR_EQUIVALENT_INFORMATION_CAPABILITY';
  if (value === false) return 'NO_RECREATION_CAPABILITY_DECLARED';
  if (value === 'unknown_at_application_level') return 'APPLICATION_LEVEL_UNKNOWN';
  return 'POSSIBLE_NOT_YET_CHARACTERIZED';
}

function shortestReachablePaths({
  targetId,
  adjacency,
  direction,
}) {
  const best = new Map();
  const queue = [{ nodeId: targetId, objectPath: [targetId], edges: [] }];
  const bestDepth = new Map([[targetId, 0]]);

  while (queue.length > 0) {
    const current = queue.shift();
    const candidates = adjacency.get(current.nodeId) ?? [];
    for (const edge of candidates) {
      const nextId = direction === 'incoming' ? edge.from : edge.to;
      const nextDepth = current.edges.length + 1;
      const knownDepth = bestDepth.get(nextId);
      if (knownDepth !== undefined && knownDepth < nextDepth) continue;

      const objectPath = direction === 'incoming'
        ? [nextId, ...current.objectPath]
        : [...current.objectPath, nextId];
      const edgePath = direction === 'incoming'
        ? [edge, ...current.edges]
        : [...current.edges, edge];
      const candidate = { nodeId: nextId, objectPath, edges: edgePath };
      const existing = best.get(nextId);
      const candidateKey = JSON.stringify([
        candidate.objectPath,
        candidate.edges.map((item) => item.relationship),
      ]);
      const existingKey = existing === undefined
        ? null
        : JSON.stringify([
            existing.objectPath,
            existing.edges.map((item) => item.relationship),
          ]);
      if (
        existing === undefined
        || candidate.edges.length < existing.edges.length
        || (candidate.edges.length === existing.edges.length && candidateKey < existingKey)
      ) {
        best.set(nextId, candidate);
      }
      if (knownDepth === undefined || nextDepth < knownDepth) {
        bestDepth.set(nextId, nextDepth);
        queue.push(candidate);
      }
    }
  }

  best.delete(targetId);
  return best;
}

function pathView(path, nodes, survivingIds) {
  const source = nodes.get(path.nodeId);
  return Object.freeze({
    object_id: path.nodeId,
    survives_assumed_deletion: survivingIds.has(path.nodeId),
    recreation_capability: source.recreation_capability,
    recreation_status: recreationStatus(source.recreation_capability),
    object_path: Object.freeze([...path.objectPath]),
    relationship_path: Object.freeze(path.edges.map((edge) => edge.relationship)),
    evidence_ids: Object.freeze(stableUnique(path.edges.flatMap((edge) => edge.evidence_ids))),
  });
}

export function analyzeRet001Deletion(registry, deletedObjectIds) {
  validateRet001Registry(registry);
  if (!Array.isArray(deletedObjectIds) || deletedObjectIds.length === 0) {
    throw new Error('RET-001 deletion analysis requires at least one deleted object ID');
  }
  const graph = buildRet001ReconstructionGraph(registry);
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  const allIds = new Set(nodes.keys());
  const deleted = new Set(deletedObjectIds);
  if (deleted.size !== deletedObjectIds.length) {
    throw new Error('RET-001 deletion analysis object IDs must be unique');
  }
  for (const objectId of deleted) {
    if (!allIds.has(objectId)) {
      throw new Error(`RET-001 deletion analysis references unknown object ${objectId}`);
    }
  }
  const surviving = new Set([...allIds].filter((objectId) => !deleted.has(objectId)));
  const incoming = incomingEdges(graph);
  const outgoing = outgoingEdges(graph);

  const targets = [...deleted].sort().map((targetId) => {
    const upstream = shortestReachablePaths({
      targetId,
      adjacency: incoming,
      direction: 'incoming',
    });
    const downstream = shortestReachablePaths({
      targetId,
      adjacency: outgoing,
      direction: 'outgoing',
    });

    const mechanicalReentrySources = [...upstream.values()]
      .filter((path) => surviving.has(path.nodeId))
      .map((path) => pathView(path, nodes, surviving))
      .sort((left, right) => left.object_id.localeCompare(right.object_id));

    const survivingDownstreamCopies = [...downstream.values()]
      .filter((path) => surviving.has(path.nodeId))
      .map((path) => pathView(path, nodes, surviving))
      .sort((left, right) => left.object_id.localeCompare(right.object_id));

    const directReentrySources = (incoming.get(targetId) ?? [])
      .filter((edge) => surviving.has(edge.from))
      .map((edge) => Object.freeze({
        object_id: edge.from,
        relationship: edge.relationship,
        recreation_capability: nodes.get(edge.from).recreation_capability,
        recreation_status: recreationStatus(nodes.get(edge.from).recreation_capability),
        evidence_ids: Object.freeze([...edge.evidence_ids]),
      }))
      .sort((left, right) => left.object_id.localeCompare(right.object_id)
        || left.relationship.localeCompare(right.relationship));

    return Object.freeze({
      target_object_id: targetId,
      direct_reentry_sources: Object.freeze(directReentrySources),
      mechanical_reentry_sources: Object.freeze(mechanicalReentrySources),
      surviving_downstream_copies_or_derivatives: Object.freeze(survivingDownstreamCopies),
    });
  });

  return Object.freeze({
    deleted_object_ids: Object.freeze([...deleted].sort()),
    surviving_object_ids: Object.freeze([...surviving].sort()),
    targets: Object.freeze(targets),
    interpretation: Object.freeze({
      mechanical_reentry_sources:
        'surviving upstream objects with a declared reconstruction/copy/replay path into the deleted target',
      surviving_downstream_copies_or_derivatives:
        'surviving objects downstream of the deleted target that may retain equivalent identifying or semantic information; this is not by itself proof of an executable reverse restore path',
    }),
  });
}

export function buildRet001ResurrectionIndex(registry) {
  validateRet001Registry(registry);
  const analyses = {};
  for (const entry of registry.objects) {
    analyses[entry.id] = analyzeRet001Deletion(registry, [entry.id]).targets[0];
  }
  return Object.freeze({
    schema: RET001_RESURRECTION_INDEX_SCHEMA,
    product_baseline_revision: registry.product_baseline_revision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    deletion_model: 'single_object_deleted_all_other_registered_objects_survive',
    analyses: Object.freeze(analyses),
  });
}

export function buildRet001LegalMatrix(registry) {
  validateRet001Registry(registry);
  const graph = buildRet001ReconstructionGraph(registry);
  const incoming = incomingEdges(graph);
  const rows = registry.objects.map((entry) => Object.freeze({
    id: entry.id,
    system: entry.system,
    location: Object.freeze({ ...entry.location }),
    data_categories: Object.freeze([...entry.data_categories]),
    purpose: entry.purpose,
    raw_text_requirement: Object.freeze({ ...entry.raw_text_requirement }),
    stable_identities: Object.freeze([...entry.stable_identities]),
    current_behavior: structuredClone(entry.current_behavior),
    technical_capability: Object.freeze({ ...entry.technical_capability }),
    engineering_design_options: structuredClone(entry.engineering_design_options),
    legal_policy: Object.freeze({ ...entry.legal_policy }),
    reconstruction: reconstructionView(entry),
    direct_resurrection_sources: Object.freeze(
      (incoming.get(entry.id) ?? [])
        .map((edge) => Object.freeze({
          object_id: edge.from,
          relationship: edge.relationship,
          evidence_ids: Object.freeze([...edge.evidence_ids]),
        }))
        .sort((left, right) => left.object_id.localeCompare(right.object_id)
          || left.relationship.localeCompare(right.relationship)),
    ),
    source_references: Object.freeze(stableUnique(entry.source_references)),
    evidence_ids: Object.freeze(stableUnique(entry.evidence_ids)),
  }));

  return Object.freeze({
    schema: RET001_MATRIX_SCHEMA,
    product_baseline_revision: registry.product_baseline_revision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    completeness_note: registry.completeness_note,
    rows: Object.freeze(rows),
  });
}

function markdownEscape(value) {
  return String(value)
    .replaceAll('|', '\\|')
    .replaceAll('\n', '<br>');
}

function joinLines(values) {
  return values.map(markdownEscape).join('<br>');
}

function lifecycleCell(entry) {
  const operations = Object.entries(entry.current_behavior.lifecycle_operations)
    .map(([name, value]) => `${name}=${value}`);
  return joinLines([
    `create/update: ${entry.current_behavior.creation_update_lifecycle}`,
    `expiry/prune: ${entry.current_behavior.expiry_prune_behavior}`,
    ...operations,
  ]);
}

export function renderRet001LegalMatrixMarkdown(registry) {
  const matrix = buildRet001LegalMatrix(registry);
  const lines = [
    '# RET-001 Product object matrix',
    '',
    `Product factual baseline: \`${matrix.product_baseline_revision}\``,
    '',
    `Registry version: \`${matrix.registry_version}\``,
    '',
    `Inventory status: \`${matrix.inventory_status}\``,
    '',
    matrix.completeness_note,
    '',
    '| Object | Location | Raw text | Purpose | Current lifecycle | Deletion paths | Backup / restore | Selective / age capability | Atomicity | Reconstruction / resurrection | Legal policy |',
    '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |',
  ];

  for (const row of matrix.rows) {
    const reconstruction = row.reconstruction;
    const reconstructionLines = [
      `authoritative_source=${reconstruction.authoritative_source.join(', ') || 'none'}`,
      `derived_from=${reconstruction.derived_from.join(', ') || 'none'}`,
      `copies_to=${reconstruction.copies_to.join(', ') || 'none'}`,
      `rebuild_or_replay_source=${reconstruction.rebuild_or_replay_source.join(', ') || 'none'}`,
      `can_recreate=${String(reconstruction.can_recreate_content_or_equivalent_identifying_semantic_information)}`,
      `direct_sources=${row.direct_resurrection_sources.map((source) => `${source.object_id}:${source.relationship}`).join(', ') || 'none'}`,
    ];
    lines.push([
      `\`${row.id}\``,
      joinLines([row.location.physical, row.location.logical]),
      markdownEscape(row.raw_text_requirement.contains_raw_message_text),
      markdownEscape(row.purpose),
      lifecycleCell(row),
      joinLines(row.current_behavior.user_deletion_paths),
      joinLines([
        `backup: ${row.current_behavior.backup_inclusion}`,
        `restore: ${row.current_behavior.restore_behavior}`,
      ]),
      joinLines([
        `selective: ${row.technical_capability.selective_deletion_feasibility}`,
        `age: ${row.technical_capability.age_based_deletion_feasibility}`,
      ]),
      markdownEscape(row.technical_capability.atomicity_boundary),
      joinLines(reconstructionLines),
      joinLines([
        `status=${row.legal_policy.status}`,
        `retention=${row.legal_policy.retention_period}`,
        `deletion=${row.legal_policy.required_deletion_behavior}`,
        `minimum=${row.legal_policy.minimum_retention}`,
      ]),
    ].join(' | '));
  }

  return `${lines.join('\n')}\n`;
}

export function canonicalGeneratedJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}
