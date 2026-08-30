import { readFile } from 'node:fs/promises';

import { validateRet001Registry } from './ret-001-registry.mjs';

export const RET001_LINKABILITY_SCHEMA = 'ofca-ret-001-derived-linkability/v1';
export const RET001_LINKABILITY_STATUSES = Object.freeze([
  'PHASE_3_IN_PROGRESS',
  'COMPLETE',
]);
export const RET001_DERIVED_STORE_LOCATIONS = Object.freeze([
  'analytics-projections.sqlite3',
  'projections.sqlite3',
]);

const RAW_TEXT_STATUSES = new Set(['ABSENT', 'PRESENT', 'PRESENT_SUBSET']);
const IDENTIFIER_STATUSES = new Set([
  'ABSENT',
  'DIRECT',
  'SERIALIZED_DIRECT',
  'OPAQUE_STABLE',
  'OPAQUE_GRAPH_IDENTITY',
]);
const JOINABILITY_STATUSES = new Set([
  'ACCOUNT_ONLY',
  'DETERMINISTIC_OPAQUE_REF',
  'DIRECT_STABLE_IDENTIFIER',
  'MIXED',
  'NONE',
]);
const SEMANTIC_STATUSES = new Set(['ABSENT', 'PRESENT', 'PENDING']);
const INFERENCE_CLASSES = new Set([
  'COORDINATION_ONLY',
  'RAW_CONTENT_COPY',
  'SEMANTIC_DERIVATIVE',
  'STRONG_SEMANTIC_INFERENCE',
]);
const UTILITY_STATUSES = new Set([
  'NO_MEANINGFUL_ANALYTICS_UTILITY',
  'RETAINS_REDUCED_UTILITY',
  'RETAINS_SUBSTANTIAL_UTILITY',
  'PENDING',
]);
const CONSISTENCY_STATUSES = new Set(['MATCH', 'MISMATCH']);
const SHA_PATTERN = /^[0-9a-f]{40}$/;

function invariant(condition, message) {
  if (!condition) throw new Error(`RET-001 linkability invalid: ${message}`);
}

function string(value, label) {
  invariant(typeof value === 'string' && value.length > 0, `${label} must be a non-empty string`);
}

function stringArray(value, label, { nonEmpty = false } = {}) {
  invariant(Array.isArray(value), `${label} must be an array`);
  if (nonEmpty) invariant(value.length > 0, `${label} must not be empty`);
  for (const item of value) string(item, `${label}[]`);
  invariant(new Set(value).size === value.length, `${label} must not contain duplicates`);
}

function exactKeys(value, expected, label) {
  invariant(value !== null && typeof value === 'object' && !Array.isArray(value), `${label} must be an object`);
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  invariant(JSON.stringify(actual) === JSON.stringify(wanted), `${label} keys differ: ${actual.join(', ')}`);
}

function derivedObjectIds(registry) {
  return registry.objects
    .filter((entry) => RET001_DERIVED_STORE_LOCATIONS.includes(entry.location.physical))
    .map((entry) => entry.id)
    .sort();
}

function validateEvidenceCatalog(classification, registry) {
  const catalog = classification.evidence_catalog;
  invariant(catalog !== null && typeof catalog === 'object' && !Array.isArray(catalog), 'evidence_catalog must be an object');
  const ids = Object.keys(catalog);
  invariant(ids.length > 0, 'evidence_catalog must not be empty');
  invariant(JSON.stringify(ids) === JSON.stringify([...ids].sort()), 'evidence_catalog keys must be sorted');
  for (const id of ids) {
    exactKeys(catalog[id], ['path', 'revision'], `evidence_catalog.${id}`);
    string(catalog[id].path, `evidence_catalog.${id}.path`);
    invariant(catalog[id].revision === registry.product_baseline_revision, `${id}.revision must equal registry baseline`);
  }
  return new Set(ids);
}

function validateIdentifier(value, label) {
  exactKeys(value, ['status', 'basis'], label);
  invariant(IDENTIFIER_STATUSES.has(value.status), `${label}.status is invalid`);
  string(value.basis, `${label}.basis`);
}

function validateClassification(entry, registryObject, evidenceIds, classification) {
  exactKeys(entry, [
    'object_id',
    'raw_text_presence',
    'identifiers',
    'canonical_joinability',
    'unique_participant_semantics',
    'inference_or_reconstruction_capability',
    'usefulness_after_identifier_removal',
    'registry_consistency',
    'classification_limitations',
    'source_references',
    'evidence_ids',
  ], `classifications.${entry.object_id ?? '<missing>'}`);

  invariant(entry.object_id === registryObject.id, `${entry.object_id}.object_id does not match registry object`);

  exactKeys(entry.raw_text_presence, ['status', 'scope', 'basis'], `${entry.object_id}.raw_text_presence`);
  invariant(RAW_TEXT_STATUSES.has(entry.raw_text_presence.status), `${entry.object_id}.raw_text_presence.status is invalid`);
  string(entry.raw_text_presence.scope, `${entry.object_id}.raw_text_presence.scope`);
  string(entry.raw_text_presence.basis, `${entry.object_id}.raw_text_presence.basis`);

  exactKeys(entry.identifiers, ['message', 'conversation', 'participant'], `${entry.object_id}.identifiers`);
  validateIdentifier(entry.identifiers.message, `${entry.object_id}.identifiers.message`);
  validateIdentifier(entry.identifiers.conversation, `${entry.object_id}.identifiers.conversation`);
  validateIdentifier(entry.identifiers.participant, `${entry.object_id}.identifiers.participant`);

  exactKeys(entry.canonical_joinability, ['status', 'basis'], `${entry.object_id}.canonical_joinability`);
  invariant(JOINABILITY_STATUSES.has(entry.canonical_joinability.status), `${entry.object_id}.canonical_joinability.status is invalid`);
  string(entry.canonical_joinability.basis, `${entry.object_id}.canonical_joinability.basis`);

  exactKeys(entry.unique_participant_semantics, ['status', 'basis'], `${entry.object_id}.unique_participant_semantics`);
  invariant(SEMANTIC_STATUSES.has(entry.unique_participant_semantics.status), `${entry.object_id}.unique_participant_semantics.status is invalid`);
  string(entry.unique_participant_semantics.basis, `${entry.object_id}.unique_participant_semantics.basis`);

  exactKeys(entry.inference_or_reconstruction_capability, [
    'classification',
    'can_reconstruct_raw_text',
    'can_strongly_infer_source_information',
    'basis',
  ], `${entry.object_id}.inference_or_reconstruction_capability`);
  invariant(INFERENCE_CLASSES.has(entry.inference_or_reconstruction_capability.classification), `${entry.object_id}.inference classification is invalid`);
  invariant(
    typeof entry.inference_or_reconstruction_capability.can_reconstruct_raw_text === 'boolean'
      || entry.inference_or_reconstruction_capability.can_reconstruct_raw_text === 'SUBSET_ONLY',
    `${entry.object_id}.can_reconstruct_raw_text must be boolean or SUBSET_ONLY`,
  );
  invariant(typeof entry.inference_or_reconstruction_capability.can_strongly_infer_source_information === 'boolean', `${entry.object_id}.can_strongly_infer_source_information must be boolean`);
  string(entry.inference_or_reconstruction_capability.basis, `${entry.object_id}.inference_or_reconstruction_capability.basis`);

  exactKeys(entry.usefulness_after_identifier_removal, ['status', 'basis'], `${entry.object_id}.usefulness_after_identifier_removal`);
  invariant(UTILITY_STATUSES.has(entry.usefulness_after_identifier_removal.status), `${entry.object_id}.usefulness_after_identifier_removal.status is invalid`);
  string(entry.usefulness_after_identifier_removal.basis, `${entry.object_id}.usefulness_after_identifier_removal.basis`);

  exactKeys(entry.registry_consistency, ['status', 'discrepancies'], `${entry.object_id}.registry_consistency`);
  invariant(CONSISTENCY_STATUSES.has(entry.registry_consistency.status), `${entry.object_id}.registry_consistency.status is invalid`);
  stringArray(entry.registry_consistency.discrepancies, `${entry.object_id}.registry_consistency.discrepancies`);
  invariant(
    entry.registry_consistency.status === 'MISMATCH'
      ? entry.registry_consistency.discrepancies.length > 0
      : entry.registry_consistency.discrepancies.length === 0,
    `${entry.object_id}.registry_consistency discrepancy count contradicts status`,
  );

  const registryRawText = registryObject.raw_text_requirement.contains_raw_message_text;
  const classificationHasRawText = entry.raw_text_presence.status !== 'ABSENT';
  const registrySaysRawText = registryRawText === true;
  const rawTextMismatch = registryRawText !== 'unknown_at_application_level'
    && classificationHasRawText !== registrySaysRawText;
  invariant(
    rawTextMismatch === (entry.registry_consistency.status === 'MISMATCH'),
    `${entry.object_id}.registry_consistency must truthfully reflect raw-text classification mismatch`,
  );

  stringArray(entry.classification_limitations, `${entry.object_id}.classification_limitations`, { nonEmpty: true });
  stringArray(entry.source_references, `${entry.object_id}.source_references`, { nonEmpty: true });
  stringArray(entry.evidence_ids, `${entry.object_id}.evidence_ids`, { nonEmpty: true });
  for (const evidenceId of entry.evidence_ids) {
    invariant(evidenceIds.has(evidenceId), `${entry.object_id} references unknown evidence ID ${evidenceId}`);
    invariant(
      entry.source_references.includes(classification.evidence_catalog[evidenceId].path),
      `${entry.object_id} must list source path for evidence ${evidenceId}`,
    );
  }
}

export function validateRet001Linkability(classification, registry) {
  validateRet001Registry(registry);
  exactKeys(classification, [
    'schema',
    'classification_version',
    'product_baseline_revision',
    'registry_version',
    'classification_status',
    'scope',
    'evidence_catalog',
    'classifications',
  ], 'linkability');
  invariant(classification.schema === RET001_LINKABILITY_SCHEMA, 'schema identifier differs');
  invariant(classification.classification_version === 1, 'classification_version must be 1');
  invariant(SHA_PATTERN.test(classification.product_baseline_revision), 'product_baseline_revision must be lowercase 40-hex');
  invariant(classification.product_baseline_revision === registry.product_baseline_revision, 'product baseline must equal registry baseline');
  invariant(classification.registry_version === registry.registry_version, 'registry_version must equal canonical registry version');
  invariant(RET001_LINKABILITY_STATUSES.includes(classification.classification_status), 'classification_status is invalid');

  exactKeys(classification.scope, [
    'derived_store_physical_locations',
    'legal_policy_selected',
    'production_lifecycle_changes_authorized',
  ], 'scope');
  invariant(
    JSON.stringify(classification.scope.derived_store_physical_locations)
      === JSON.stringify([...RET001_DERIVED_STORE_LOCATIONS]),
    'derived_store_physical_locations must match the Phase-3 derived-store boundary',
  );
  invariant(classification.scope.legal_policy_selected === false, 'Phase 3 must not select Legal policy');
  invariant(classification.scope.production_lifecycle_changes_authorized === false, 'Phase 3 must not authorize production lifecycle changes');

  const evidenceIds = validateEvidenceCatalog(classification, registry);
  invariant(Array.isArray(classification.classifications), 'classifications must be an array');
  const requiredIds = derivedObjectIds(registry);
  const actualIds = classification.classifications.map((entry) => entry.object_id);
  invariant(new Set(actualIds).size === actualIds.length, 'classification object IDs must be unique');
  invariant(JSON.stringify(actualIds) === JSON.stringify([...actualIds].sort()), 'classifications must be sorted by object_id');
  invariant(
    JSON.stringify(actualIds) === JSON.stringify(requiredIds),
    `classification coverage differs from canonical derived objects; expected ${requiredIds.join(', ')}, got ${actualIds.join(', ')}`,
  );

  const registryById = new Map(registry.objects.map((entry) => [entry.id, entry]));
  for (const entry of classification.classifications) {
    validateClassification(entry, registryById.get(entry.object_id), evidenceIds, classification);
  }
  return classification;
}

export function buildRet001LinkabilityAudit(classification, registry) {
  validateRet001Linkability(classification, registry);
  const mismatchObjectIds = classification.classifications
    .filter((entry) => entry.registry_consistency.status === 'MISMATCH')
    .map((entry) => entry.object_id);
  const pendingObjectIds = classification.classifications
    .filter((entry) => (
      entry.unique_participant_semantics.status === 'PENDING'
      || entry.usefulness_after_identifier_removal.status === 'PENDING'
    ))
    .map((entry) => entry.object_id);
  const completenessGate = registry.inventory_status === 'COMPLETE'
    ? (mismatchObjectIds.length === 0 && pendingObjectIds.length === 0 ? 'PASS' : 'FAIL')
    : 'DEFERRED_UNTIL_INVENTORY_COMPLETE';
  return Object.freeze({
    schema: 'ofca-ret-001-linkability-audit/v1',
    product_baseline_revision: registry.product_baseline_revision,
    registry_version: registry.registry_version,
    inventory_status: registry.inventory_status,
    classification_status: classification.classification_status,
    derived_object_count: classification.classifications.length,
    mismatch_object_ids: Object.freeze(mismatchObjectIds),
    pending_object_ids: Object.freeze(pendingObjectIds),
    completeness_gate: completenessGate,
  });
}

export function assertRet001LinkabilityCompleteness(classification, registry) {
  const audit = buildRet001LinkabilityAudit(classification, registry);
  if (audit.completeness_gate === 'FAIL') {
    throw new Error(
      `RET-001 linkability completeness failed: registry mismatches=${audit.mismatch_object_ids.join(',') || 'none'} pending=${audit.pending_object_ids.join(',') || 'none'}`,
    );
  }
  return audit;
}

export async function loadRet001Linkability(
  registry,
  url = new URL('./ret-001-linkability.json', import.meta.url),
) {
  const text = await readFile(url, 'utf8');
  const classification = JSON.parse(text);
  validateRet001Linkability(classification, registry);
  return { classification, text };
}

export function renderRet001LinkabilityMarkdown(classification, registry) {
  const audit = buildRet001LinkabilityAudit(classification, registry);
  const lines = [
    '# RET-001 derived-data linkability classification',
    '',
    `Product factual baseline: \`${registry.product_baseline_revision}\``,
    '',
    `Inventory status: \`${registry.inventory_status}\``,
    '',
    `Linkability completeness gate: \`${audit.completeness_gate}\``,
    '',
    '| Object | Raw text | Message ID | Conversation ID | Participant ID | Canonical joinability | Participant semantics | Inference / reconstruction | Utility after identifier removal | Registry consistency |',
    '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |',
  ];
  for (const entry of classification.classifications) {
    lines.push([
      `\`${entry.object_id}\``,
      entry.raw_text_presence.status,
      entry.identifiers.message.status,
      entry.identifiers.conversation.status,
      entry.identifiers.participant.status,
      entry.canonical_joinability.status,
      entry.unique_participant_semantics.status,
      `${entry.inference_or_reconstruction_capability.classification}; raw=${String(entry.inference_or_reconstruction_capability.can_reconstruct_raw_text)}; strong_inference=${String(entry.inference_or_reconstruction_capability.can_strongly_infer_source_information)}`,
      entry.usefulness_after_identifier_removal.status,
      entry.registry_consistency.status,
    ].join(' | '));
  }
  return `${lines.join('\n')}\n`;
}
