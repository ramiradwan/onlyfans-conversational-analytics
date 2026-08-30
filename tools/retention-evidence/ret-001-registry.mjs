import { readFile } from 'node:fs/promises';

export const RET001_REGISTRY_SCHEMA = 'ofca-ret-001-object-registry/v1';
export const RET001_REGISTRY_OVERLAY_SCHEMA = 'ofca-ret-001-object-registry-overlay/v1';
export const RET001_REGISTRY_OVERLAY_FRAGMENT_SCHEMA = 'ofca-ret-001-object-registry-overlay-fragment/v1';
export const RET001_NO_ENGINEERING_MINIMUM = 'No engineering minimum established.';
export const RET001_LEGAL_POLICY_STATUS = 'UNRESOLVED';
export const RET001_INVENTORY_STATUSES = Object.freeze(['PHASE_1_SEED_INCOMPLETE', 'COMPLETE']);
export const RET001_REQUIRED_LIFECYCLE_OPERATIONS = Object.freeze([
  'history_revocation', 'pause', 'downgrade', 'account_disconnect',
  'extension_delete_all', 'companion_dataset_deletion', 'uninstall',
]);
export const RET001_RECONSTRUCTION_FIELDS = Object.freeze([
  'authoritative_source_object_ids', 'derived_from_object_ids',
  'copies_to_object_ids', 'rebuild_or_replay_source_object_ids',
]);

const ID_PATTERN = /^(?:EXT|COMP)-[A-Z0-9]+(?:-[A-Z0-9]+)*$/;
const SHA_PATTERN = /^[0-9a-f]{40}$/;

function invariant(condition, message) {
  if (!condition) throw new Error(`RET-001 object registry invalid: ${message}`);
}
function string(value, label) { invariant(typeof value === 'string' && value.length > 0, `${label} must be a non-empty string`); }
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

function validateEvidenceCatalog(registry) {
  const catalog = registry.evidence_catalog;
  invariant(catalog !== null && typeof catalog === 'object' && !Array.isArray(catalog), 'evidence_catalog must be an object');
  const ids = Object.keys(catalog);
  invariant(ids.length > 0, 'evidence_catalog must not be empty');
  invariant(JSON.stringify(ids) === JSON.stringify([...ids].sort()), 'evidence_catalog keys must be sorted');
  for (const id of ids) {
    exactKeys(catalog[id], ['path', 'revision'], `evidence_catalog.${id}`);
    string(catalog[id].path, `evidence_catalog.${id}.path`);
    invariant(catalog[id].revision === registry.product_baseline_revision, `evidence_catalog.${id}.revision must equal product baseline`);
  }
  return new Set(ids);
}

function validateObject(entry, allIds, evidenceIds, registry) {
  exactKeys(entry, [
    'id','system','location','data_categories','purpose','raw_text_requirement','stable_identities',
    'current_behavior','technical_capability','engineering_design_options','legal_policy',
    'reconstruction','source_references','evidence_ids',
  ], `objects.${entry.id ?? '<missing>'}`);
  string(entry.id, 'object.id');
  invariant(ID_PATTERN.test(entry.id), `${entry.id} is not a stable RET-001 object ID`);
  invariant(entry.system === 'extension' || entry.system === 'companion', `${entry.id}.system must be extension or companion`);
  exactKeys(entry.location, ['physical', 'logical'], `${entry.id}.location`);
  string(entry.location.physical, `${entry.id}.location.physical`);
  string(entry.location.logical, `${entry.id}.location.logical`);
  stringArray(entry.data_categories, `${entry.id}.data_categories`, { nonEmpty: true });
  string(entry.purpose, `${entry.id}.purpose`);
  stringArray(entry.stable_identities, `${entry.id}.stable_identities`, { nonEmpty: true });
  exactKeys(entry.raw_text_requirement, ['contains_raw_message_text','necessary_after_ingestion_or_analysis','minimum_operational_retention'], `${entry.id}.raw_text_requirement`);
  invariant(typeof entry.raw_text_requirement.contains_raw_message_text === 'boolean' || entry.raw_text_requirement.contains_raw_message_text === 'unknown_at_application_level', `${entry.id}.raw_text_requirement.contains_raw_message_text must be boolean or unknown_at_application_level`);
  string(entry.raw_text_requirement.necessary_after_ingestion_or_analysis, `${entry.id}.raw_text_requirement.necessary_after_ingestion_or_analysis`);
  invariant(entry.raw_text_requirement.minimum_operational_retention === RET001_NO_ENGINEERING_MINIMUM, `${entry.id} must use exact engineering minimum text`);
  exactKeys(entry.current_behavior, ['creation_update_lifecycle','expiry_prune_behavior','user_deletion_paths','lifecycle_operations','backup_inclusion','restore_behavior'], `${entry.id}.current_behavior`);
  string(entry.current_behavior.creation_update_lifecycle, `${entry.id}.current_behavior.creation_update_lifecycle`);
  string(entry.current_behavior.expiry_prune_behavior, `${entry.id}.current_behavior.expiry_prune_behavior`);
  stringArray(entry.current_behavior.user_deletion_paths, `${entry.id}.current_behavior.user_deletion_paths`, { nonEmpty: true });
  exactKeys(entry.current_behavior.lifecycle_operations, RET001_REQUIRED_LIFECYCLE_OPERATIONS, `${entry.id}.current_behavior.lifecycle_operations`);
  for (const operation of RET001_REQUIRED_LIFECYCLE_OPERATIONS) string(entry.current_behavior.lifecycle_operations[operation], `${entry.id}.current_behavior.lifecycle_operations.${operation}`);
  string(entry.current_behavior.backup_inclusion, `${entry.id}.current_behavior.backup_inclusion`);
  string(entry.current_behavior.restore_behavior, `${entry.id}.current_behavior.restore_behavior`);
  exactKeys(entry.technical_capability, ['selective_deletion_feasibility','age_based_deletion_feasibility','atomicity_boundary'], `${entry.id}.technical_capability`);
  for (const [name, value] of Object.entries(entry.technical_capability)) string(value, `${entry.id}.technical_capability.${name}`);
  exactKeys(entry.engineering_design_options, ['status','options'], `${entry.id}.engineering_design_options`);
  invariant(entry.engineering_design_options.status === 'not_selected', `${entry.id} must not select an engineering design option during characterization`);
  invariant(Array.isArray(entry.engineering_design_options.options), `${entry.id}.engineering_design_options.options must be an array`);
  exactKeys(entry.legal_policy, ['status','retention_period','required_deletion_behavior','minimum_retention'], `${entry.id}.legal_policy`);
  invariant(entry.legal_policy.status === RET001_LEGAL_POLICY_STATUS, `${entry.id}.legal_policy.status must remain UNRESOLVED`);
  invariant(entry.legal_policy.retention_period === 'UNRESOLVED', `${entry.id}.legal_policy.retention_period must remain UNRESOLVED`);
  invariant(entry.legal_policy.required_deletion_behavior === 'UNRESOLVED', `${entry.id}.legal_policy.required_deletion_behavior must remain UNRESOLVED`);
  invariant(entry.legal_policy.minimum_retention === RET001_NO_ENGINEERING_MINIMUM, `${entry.id}.legal_policy.minimum_retention must use exact engineering minimum text`);
  exactKeys(entry.reconstruction, [...RET001_RECONSTRUCTION_FIELDS,'can_recreate_deleted_content_or_identifying_semantics'], `${entry.id}.reconstruction`);
  for (const field of RET001_RECONSTRUCTION_FIELDS) {
    stringArray(entry.reconstruction[field], `${entry.id}.reconstruction.${field}`);
    for (const objectId of entry.reconstruction[field]) {
      invariant(allIds.has(objectId), `${entry.id}.reconstruction.${field} references unknown object ${objectId}`);
      invariant(objectId !== entry.id, `${entry.id}.reconstruction.${field} must not self-reference`);
    }
  }
  const recreate = entry.reconstruction.can_recreate_deleted_content_or_identifying_semantics;
  invariant(typeof recreate === 'boolean' || recreate === 'not_yet_characterized' || recreate === 'unknown_at_application_level', `${entry.id}.reconstruction.can_recreate_deleted_content_or_identifying_semantics has invalid value`);
  stringArray(entry.source_references, `${entry.id}.source_references`, { nonEmpty: true });
  stringArray(entry.evidence_ids, `${entry.id}.evidence_ids`, { nonEmpty: true });
  for (const evidenceId of entry.evidence_ids) {
    invariant(evidenceIds.has(evidenceId), `${entry.id} references unknown evidence ID ${evidenceId}`);
    invariant(entry.source_references.includes(registry.evidence_catalog[evidenceId].path), `${entry.id} must list source path for evidence ${evidenceId}`);
  }
}

export function validateRet001Registry(registry) {
  exactKeys(registry, ['schema','registry_version','product_baseline_revision','inventory_status','completeness_note','scope','constants','evidence_catalog','objects'], 'registry');
  invariant(registry.schema === RET001_REGISTRY_SCHEMA, 'schema identifier differs');
  invariant(registry.registry_version === 1, 'registry_version must be 1');
  invariant(SHA_PATTERN.test(registry.product_baseline_revision), 'product_baseline_revision must be lowercase 40-hex');
  invariant(RET001_INVENTORY_STATUSES.includes(registry.inventory_status), 'inventory_status is invalid');
  string(registry.completeness_note, 'completeness_note');
  exactKeys(registry.scope, ['issue','legal_decision','phase','production_lifecycle_changes_authorized'], 'scope');
  invariant(registry.scope.issue === 6, 'scope.issue must be 6');
  invariant(registry.scope.legal_decision === 'RET-001', 'scope.legal_decision must be RET-001');
  invariant(registry.scope.phase === 'characterization', 'scope.phase must be characterization');
  invariant(registry.scope.production_lifecycle_changes_authorized === false, 'phase 1 must not authorize production lifecycle changes');
  exactKeys(registry.constants, ['unsupported_engineering_minimum','legal_policy_status'], 'constants');
  invariant(registry.constants.unsupported_engineering_minimum === RET001_NO_ENGINEERING_MINIMUM, 'unsupported engineering minimum text differs');
  invariant(registry.constants.legal_policy_status === RET001_LEGAL_POLICY_STATUS, 'legal policy status differs');
  const evidenceIds = validateEvidenceCatalog(registry);
  invariant(Array.isArray(registry.objects) && registry.objects.length > 0, 'objects must be a non-empty array');
  const ids = registry.objects.map((entry) => entry.id);
  invariant(new Set(ids).size === ids.length, 'object IDs must be unique');
  invariant(JSON.stringify(ids) === JSON.stringify([...ids].sort()), 'objects must be sorted by stable ID');
  const allIds = new Set(ids);
  for (const entry of registry.objects) validateObject(entry, allIds, evidenceIds, registry);
  return registry;
}

function mergePatch(target, patch) {
  if (patch === null || typeof patch !== 'object' || Array.isArray(patch)) return structuredClone(patch);
  const result = structuredClone(target);
  for (const [key, value] of Object.entries(patch)) {
    result[key] = value !== null && typeof value === 'object' && !Array.isArray(value)
      && result[key] !== null && typeof result[key] === 'object' && !Array.isArray(result[key])
      ? mergePatch(result[key], value)
      : structuredClone(value);
  }
  return result;
}

function validateOverlay(overlay) {
  exactKeys(overlay, [
    'schema','registry_version','seed_file','seed_blob_sha','expected_seed_inventory_status',
    'inventory_status','completeness_note','fragment_files',
  ], 'registry overlay');
  invariant(overlay.schema === RET001_REGISTRY_OVERLAY_SCHEMA, 'overlay schema identifier differs');
  invariant(overlay.registry_version === 1, 'overlay registry_version must be 1');
  string(overlay.seed_file, 'registry overlay.seed_file');
  invariant(SHA_PATTERN.test(overlay.seed_blob_sha), 'registry overlay.seed_blob_sha must be lowercase 40-hex');
  invariant(overlay.expected_seed_inventory_status === 'PHASE_1_SEED_INCOMPLETE', 'registry overlay seed status differs');
  invariant(overlay.inventory_status === 'COMPLETE', 'registry overlay must promote inventory_status to COMPLETE');
  string(overlay.completeness_note, 'registry overlay.completeness_note');
  stringArray(overlay.fragment_files, 'registry overlay.fragment_files', { nonEmpty: true });
  return overlay;
}

function validateOverlayFragment(fragment) {
  exactKeys(fragment, ['schema','evidence_catalog_additions','object_patches','added_objects'], 'registry overlay fragment');
  invariant(fragment.schema === RET001_REGISTRY_OVERLAY_FRAGMENT_SCHEMA, 'overlay fragment schema identifier differs');
  invariant(fragment.evidence_catalog_additions && typeof fragment.evidence_catalog_additions === 'object' && !Array.isArray(fragment.evidence_catalog_additions), 'registry overlay fragment evidence_catalog_additions must be an object');
  invariant(fragment.object_patches && typeof fragment.object_patches === 'object' && !Array.isArray(fragment.object_patches), 'registry overlay fragment object_patches must be an object');
  invariant(Array.isArray(fragment.added_objects), 'registry overlay fragment added_objects must be an array');
  return fragment;
}

async function resolveOverlay(overlay, overlayUrl) {
  validateOverlay(overlay);
  const seedUrl = new URL(overlay.seed_file, overlayUrl);
  const seed = JSON.parse(await readFile(seedUrl, 'utf8'));
  validateRet001Registry(seed);
  invariant(seed.registry_version === overlay.registry_version, 'overlay registry version differs from seed');
  invariant(seed.inventory_status === overlay.expected_seed_inventory_status, 'overlay seed inventory status differs');
  const registry = structuredClone(seed);
  registry.inventory_status = overlay.inventory_status;
  registry.completeness_note = overlay.completeness_note;
  const byId = new Map(registry.objects.map((entry) => [entry.id, entry]));
  const seenEvidence = new Set();
  const seenPatches = new Set();
  const seenAdded = new Set();
  for (const fragmentFile of overlay.fragment_files) {
    const fragmentUrl = new URL(fragmentFile, overlayUrl);
    const fragment = validateOverlayFragment(JSON.parse(await readFile(fragmentUrl, 'utf8')));
    for (const [evidenceId, evidence] of Object.entries(fragment.evidence_catalog_additions)) {
      invariant(!seenEvidence.has(evidenceId), `overlay evidence ${evidenceId} is duplicated across fragments`);
      seenEvidence.add(evidenceId);
      invariant(registry.evidence_catalog[evidenceId] === undefined, `overlay evidence ${evidenceId} already exists in seed`);
      registry.evidence_catalog[evidenceId] = structuredClone(evidence);
    }
    for (const [objectId, patch] of Object.entries(fragment.object_patches)) {
      invariant(!seenPatches.has(objectId), `overlay patch ${objectId} is duplicated across fragments`);
      seenPatches.add(objectId);
      invariant(byId.has(objectId), `overlay patch references unknown seed object ${objectId}`);
      byId.set(objectId, mergePatch(byId.get(objectId), patch));
    }
    for (const entry of fragment.added_objects) {
      invariant(!seenAdded.has(entry.id), `overlay added object ${entry.id} is duplicated across fragments`);
      seenAdded.add(entry.id);
      invariant(!byId.has(entry.id), `overlay added object already exists in seed: ${entry.id}`);
      byId.set(entry.id, structuredClone(entry));
    }
  }
  registry.evidence_catalog = Object.fromEntries(Object.entries(registry.evidence_catalog).sort(([left], [right]) => left.localeCompare(right)));
  registry.objects = [...byId.values()].sort((left, right) => left.id.localeCompare(right.id));
  validateRet001Registry(registry);
  return registry;
}

export async function loadRet001Registry(url = new URL('./ret-001-objects.json', import.meta.url)) {
  const sourceText = await readFile(url, 'utf8');
  const document = JSON.parse(sourceText);
  const registry = document.schema === RET001_REGISTRY_OVERLAY_SCHEMA
    ? await resolveOverlay(document, url)
    : validateRet001Registry(document);
  const text = canonicalRet001RegistryJson(registry);
  return { registry, text, sourceText };
}

export function canonicalRet001RegistryJson(registry) {
  validateRet001Registry(registry);
  return `${JSON.stringify(registry, null, 2)}\n`;
}
