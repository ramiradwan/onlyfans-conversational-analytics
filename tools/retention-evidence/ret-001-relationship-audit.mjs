import { validateRet001Registry } from './ret-001-registry.mjs';
import {
  analyzeRet001Deletion,
  buildRet001ReconstructionGraph,
} from './ret-001-views.mjs';

export const RET001_RELATIONSHIP_AUDIT_SCHEMA = 'ofca-ret-001-relationship-audit/v1';
export const RET001_RESURRECTION_ANSWERS_SCHEMA = 'ofca-ret-001-resurrection-answers/v1';

function stableUnique(values) {
  return [...new Set(values)].sort();
}

function findingSort(left, right) {
  return left.severity.localeCompare(right.severity)
    || left.code.localeCompare(right.code)
    || String(left.object_id ?? '').localeCompare(String(right.object_id ?? ''))
    || String(left.edge_key ?? '').localeCompare(String(right.edge_key ?? ''));
}

function edgeKey(edge) {
  return `${edge.from}|${edge.relationship}|${edge.to}`;
}

function relationCounts(graph) {
  const incoming = new Map(graph.nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(graph.nodes.map((node) => [node.id, 0]));
  for (const edge of graph.edges) {
    incoming.set(edge.to, incoming.get(edge.to) + 1);
    outgoing.set(edge.from, outgoing.get(edge.from) + 1);
  }
  return { incoming, outgoing };
}

function classifyRelationshipPath(relationshipPath) {
  const relationships = new Set(relationshipPath);
  if (relationships.has('rebuild_or_replay_source')) {
    return Object.freeze({
      path_kind: 'REBUILD_OR_REPLAY_PATH_DECLARED',
      execution_evidence: 'REBUILD_OR_REPLAY_RELATIONSHIP_DECLARED',
    });
  }
  if (relationships.has('copies_to')) {
    return Object.freeze({
      path_kind: 'COPY_FLOW_PATH_DECLARED',
      execution_evidence: 'COPY_RELATIONSHIP_DECLARED',
    });
  }
  if (
    relationships.size > 0
    && [...relationships].every(
      (relationship) => relationship === 'authoritative_source' || relationship === 'derived_from',
    )
  ) {
    return Object.freeze({
      path_kind: 'STRUCTURAL_SOURCE_OR_DERIVATION_PATH',
      execution_evidence: 'EXECUTABLE_REENTRY_NOT_ESTABLISHED_BY_RELATIONSHIP_TYPE',
    });
  }
  return Object.freeze({
    path_kind: 'MIXED_DECLARED_RELATIONSHIP_PATH',
    execution_evidence: 'EXECUTABLE_REENTRY_REQUIRES_SCENARIO_CHARACTERIZATION',
  });
}

export function buildRet001RelationshipAudit(registry) {
  validateRet001Registry(registry);
  const graph = buildRet001ReconstructionGraph(registry);
  const { incoming, outgoing } = relationCounts(graph);
  const findings = [];
  const exactEdges = new Set();

  for (const edge of graph.edges) {
    const key = edgeKey(edge);
    if (exactEdges.has(key)) {
      findings.push(Object.freeze({
        severity: 'BLOCKING',
        code: 'DUPLICATE_RELATIONSHIP_EDGE',
        edge_key: key,
        object_id: edge.declared_by_object_id,
        detail: 'The canonical registry produces the same directed relationship edge more than once.',
      }));
    }
    exactEdges.add(key);
    if (edge.from === edge.to) {
      findings.push(Object.freeze({
        severity: 'BLOCKING',
        code: 'SELF_RELATIONSHIP_EDGE',
        edge_key: key,
        object_id: edge.declared_by_object_id,
        detail: 'A reconstruction relationship must not point from an object back to itself.',
      }));
    }
    if (edge.evidence_ids.length === 0 || edge.source_references.length === 0) {
      findings.push(Object.freeze({
        severity: 'BLOCKING',
        code: 'RELATIONSHIP_WITHOUT_PROVENANCE',
        edge_key: key,
        object_id: edge.declared_by_object_id,
        detail: 'Every reconstruction edge must carry evidence IDs and source references.',
      }));
    }
  }

  for (const node of graph.nodes) {
    const incomingCount = incoming.get(node.id) ?? 0;
    const outgoingCount = outgoing.get(node.id) ?? 0;
    if (incomingCount === 0 && outgoingCount === 0) {
      findings.push(Object.freeze({
        severity: 'REVIEW',
        code: 'OBJECT_WITHOUT_RECONSTRUCTION_RELATIONSHIPS',
        object_id: node.id,
        edge_key: null,
        detail: 'No reconstruction relationship is currently declared for this durable/recoverable object.',
      }));
    }
    if (node.recreation_capability === true && outgoingCount === 0) {
      findings.push(Object.freeze({
        severity: 'BLOCKING',
        code: 'RECREATION_CAPABILITY_WITHOUT_DECLARED_TARGET_PATH',
        object_id: node.id,
        edge_key: null,
        detail: 'The object is marked capable of recreating deleted content or equivalent information but has no declared outgoing reconstruction path.',
      }));
    }
    if (node.recreation_capability === 'not_yet_characterized') {
      findings.push(Object.freeze({
        severity: 'REVIEW',
        code: 'RECREATION_CAPABILITY_NOT_YET_CHARACTERIZED',
        object_id: node.id,
        edge_key: null,
        detail: 'The object recreation capability remains uncharacterized.',
      }));
    }
    if (node.recreation_capability === 'unknown_at_application_level') {
      findings.push(Object.freeze({
        severity: 'REVIEW',
        code: 'RECREATION_CAPABILITY_APPLICATION_LEVEL_UNKNOWN',
        object_id: node.id,
        edge_key: null,
        detail: 'Application-level evidence cannot establish the physical recovery capability for this object.',
      }));
    }
  }

  findings.sort(findingSort);
  const blocking = findings.filter((finding) => finding.severity === 'BLOCKING');
  const review = findings.filter((finding) => finding.severity === 'REVIEW');
  const completenessEnforced = registry.inventory_status === 'COMPLETE';

  return Object.freeze({
    schema: RET001_RELATIONSHIP_AUDIT_SCHEMA,
    product_baseline_revision: registry.product_baseline_revision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    completeness_gate: Object.freeze({
      enforced: completenessEnforced,
      status: completenessEnforced
        ? (blocking.length === 0 ? 'PASS' : 'FAIL')
        : 'DEFERRED_UNTIL_INVENTORY_COMPLETE',
    }),
    summary: Object.freeze({
      object_count: graph.nodes.length,
      relationship_edge_count: graph.edges.length,
      blocking_finding_count: blocking.length,
      review_finding_count: review.length,
    }),
    findings: Object.freeze(findings),
  });
}

export function assertRet001RelationshipCompleteness(registry) {
  const audit = buildRet001RelationshipAudit(registry);
  if (!audit.completeness_gate.enforced) return audit;
  if (audit.completeness_gate.status !== 'PASS') {
    const codes = stableUnique(
      audit.findings
        .filter((finding) => finding.severity === 'BLOCKING')
        .map((finding) => finding.code),
    );
    throw new Error(
      `RET-001 relationship completeness failed: ${codes.join(', ') || 'unknown blocking finding'}`,
    );
  }
  return audit;
}

function targetFindingCodes(audit, objectId) {
  return stableUnique(
    audit.findings
      .filter((finding) => finding.object_id === objectId)
      .map((finding) => finding.code),
  );
}

function classifiedPath(entry) {
  const classification = classifyRelationshipPath(entry.relationship_path);
  return Object.freeze({
    object_id: entry.object_id,
    recreation_status: entry.recreation_status,
    path_kind: classification.path_kind,
    execution_evidence: classification.execution_evidence,
    object_path: Object.freeze([...entry.object_path]),
    relationship_path: Object.freeze([...entry.relationship_path]),
    evidence_ids: Object.freeze([...entry.evidence_ids]),
  });
}

export function answerRet001ResurrectionQuestion(registry, objectId) {
  validateRet001Registry(registry);
  const analysis = analyzeRet001Deletion(registry, [objectId]);
  const target = analysis.targets[0];
  const audit = buildRet001RelationshipAudit(registry);
  const declaredUpstream = target.mechanical_reentry_sources;
  const semantic = target.surviving_downstream_copies_or_derivatives;
  const classifiedUpstream = declaredUpstream.map(classifiedPath);
  const classifiedSemantic = semantic.map(classifiedPath);

  const hasReplayOrCopyPath = classifiedUpstream.some((entry) => (
    entry.path_kind === 'REBUILD_OR_REPLAY_PATH_DECLARED'
    || entry.path_kind === 'COPY_FLOW_PATH_DECLARED'
  ));

  let answerStatus = 'NO_DECLARED_REENTRY_OR_DOWNSTREAM_SURVIVOR';
  if (hasReplayOrCopyPath) answerStatus = 'DECLARED_REPLAY_OR_COPY_REENTRY_PATH_EXISTS';
  else if (classifiedUpstream.length > 0) {
    answerStatus = 'STRUCTURAL_RECONSTRUCTION_DEPENDENCY_EXISTS_EXECUTION_NOT_ESTABLISHED';
  } else if (classifiedSemantic.length > 0) {
    answerStatus = 'SEMANTIC_OR_IDENTIFYING_INFORMATION_SURVIVES_WITHOUT_DECLARED_REENTRY';
  }

  const limitations = [];
  if (registry.inventory_status !== 'COMPLETE') {
    limitations.push('REGISTRY_INVENTORY_INCOMPLETE');
  }
  limitations.push(...targetFindingCodes(audit, objectId));

  return Object.freeze({
    question: `If ${objectId} disappears, which surviving registered objects can cause it or equivalent personal information to exist again?`,
    deleted_object_id: objectId,
    answer_status: answerStatus,
    declared_reentry_sources: Object.freeze(classifiedUpstream),
    surviving_equivalent_information_holders: Object.freeze(classifiedSemantic),
    limitations: Object.freeze(stableUnique(limitations)),
  });
}

export function buildRet001ResurrectionAnswers(registry) {
  validateRet001Registry(registry);
  const answers = Object.fromEntries(
    registry.objects.map((entry) => [
      entry.id,
      answerRet001ResurrectionQuestion(registry, entry.id),
    ]),
  );
  return Object.freeze({
    schema: RET001_RESURRECTION_ANSWERS_SCHEMA,
    product_baseline_revision: registry.product_baseline_revision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    interpretation: Object.freeze({
      declared_reentry_sources:
        'surviving upstream objects with a registry-declared path; path_kind and execution_evidence state whether the relationship itself establishes replay/copy or only a structural dependency',
      surviving_equivalent_information_holders:
        'surviving downstream objects that may retain equivalent identifying or semantic information; this is not by itself proof of executable reverse restoration',
    }),
    answers: Object.freeze(answers),
  });
}
