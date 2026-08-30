import { readFile } from 'node:fs/promises';

import { ENCRYPTION_KEY_CHECK_STORE } from '../../extension/transport/encrypted-indexeddb-storage.mjs';
import { INGESTION_STORES } from '../../extension/transport/durable-outbox.mjs';
import { loadRet001Registry } from './ret-001-registry.mjs';

const ROOT = new URL('../../', import.meta.url);

export const RET001_SQL_SURFACES = Object.freeze([
  Object.freeze({ store: 'auth.sqlite3', source: 'app/persistence/auth_sql/0001_local_authentication.sql' }),
  Object.freeze({ store: 'auth.sqlite3', source: 'app/persistence/auth_sql/0002_installation_key_reference.sql' }),
  Object.freeze({ store: 'auth.sqlite3', source: 'app/persistence/auth_sql/0003_authorization_epoch.sql' }),
  Object.freeze({ store: 'auth.sqlite3', source: 'app/persistence/auth_sql/0004_grant_refresh_context.sql' }),
  Object.freeze({ store: 'auth.sqlite3', source: 'app/persistence/auth_sql/0005_membership_roles.sql' }),
  Object.freeze({ store: 'auth.sqlite3', source: 'app/persistence/auth_sql/0006_provisioning_state.sql' }),
  Object.freeze({ store: 'auth.sqlite3', source: 'app/persistence/auth_sql/0007_authorized_account_bindings.sql' }),
  Object.freeze({ store: 'auth.sqlite3', source: 'app/persistence/auth_sql/0008_claim_submissions.sql' }),
  Object.freeze({ store: 'auth.sqlite3', source: 'app/persistence/auth_sql/0009_onboarding_progress_outbox.sql' }),
  Object.freeze({ store: 'canonical.sqlite3', source: 'app/persistence/sql/0001_canonical_plane.sql' }),
  Object.freeze({ store: 'canonical.sqlite3', source: 'app/persistence/sql/0002_history_acquisition.sql' }),
  Object.freeze({ store: 'canonical.sqlite3', source: 'app/persistence/sql/0003_analytics_projection_activation.sql' }),
  Object.freeze({ store: 'canonical.sqlite3', source: 'app/persistence/sql/0004_analytics_projection_publication_identity.sql' }),
  Object.freeze({ store: 'canonical.sqlite3', source: 'app/persistence/sql/0005_analytics_projection_writer_capability.sql' }),
  Object.freeze({ store: 'canonical.sqlite3', source: 'app/persistence/sql/0006_launcher_session_handoff.sql' }),
  Object.freeze({ store: 'projections.sqlite3', source: 'app/persistence/projection_sql/0001_read_models.sql' }),
  Object.freeze({ store: 'analytics-projections.sqlite3', source: 'app/analytics/sql/0003_opaque_graph_contract.sql' }),
]);

export const RET001_AGGREGATE_SURFACE_OBJECTS = Object.freeze({
  'sql:analytics-projections.sqlite3:graph_algorithm_metrics': Object.freeze(['COMP-ANALYTICS-GRAPH']),
  'sql:analytics-projections.sqlite3:graph_edges': Object.freeze(['COMP-ANALYTICS-GRAPH']),
  'sql:analytics-projections.sqlite3:graph_nodes': Object.freeze(['COMP-ANALYTICS-GRAPH']),
  'sql:analytics-projections.sqlite3:graph_partition_stats': Object.freeze(['COMP-ANALYTICS-GRAPH']),
  'sql:canonical.sqlite3:account_coverage_heads': Object.freeze(['COMP-HISTORY-COVERAGE-STATE']),
  'sql:canonical.sqlite3:coverage_generations': Object.freeze(['COMP-HISTORY-COVERAGE-STATE']),
  'sql:canonical.sqlite3:coverage_members': Object.freeze(['COMP-HISTORY-COVERAGE-STATE']),
  'sql:canonical.sqlite3:history_settings': Object.freeze(['COMP-HISTORY-COVERAGE-STATE']),
  'sql:canonical.sqlite3:ingest_checkpoints': Object.freeze(['COMP-HISTORY-COVERAGE-STATE']),
  'sql:canonical.sqlite3:ingest_streams': Object.freeze(['COMP-HISTORY-COVERAGE-STATE']),
  'sql:canonical.sqlite3:stream_chat_membership': Object.freeze(['COMP-HISTORY-COVERAGE-STATE']),
  'sql:canonical.sqlite3:stream_epochs': Object.freeze(['COMP-HISTORY-COVERAGE-STATE']),
  'sql:canonical.sqlite3:stream_message_membership': Object.freeze(['COMP-HISTORY-COVERAGE-STATE']),
});

const REGISTERED_OBJECT = 'REGISTERED_OBJECT';
const COVERED_BY_EXISTING_OBJECT = 'COVERED_BY_EXISTING_OBJECT';
const OUTSIDE_RET001_SCOPE = 'OUTSIDE_RET001_SCOPE';
const UNVERIFIED_BELOW_SUPPORTED_BOUNDARY = 'UNVERIFIED_BELOW_SUPPORTED_BOUNDARY';
const UNEXPLAINED = 'UNEXPLAINED';

export const RET001_SOURCE_SURFACE_DISPOSITIONS = Object.freeze([
  REGISTERED_OBJECT,
  COVERED_BY_EXISTING_OBJECT,
  OUTSIDE_RET001_SCOPE,
  UNVERIFIED_BELOW_SUPPORTED_BOUNDARY,
]);

function declared(id, kind, source, sourceToken, disposition, objectIds = [], reason) {
  return Object.freeze({ id, kind, source, source_token: sourceToken, disposition, registry_object_ids: Object.freeze(objectIds), reason });
}

export const RET001_DECLARED_NON_SCHEMA_SURFACES = Object.freeze([
  declared('chrome-local:agent_installation_id', 'chrome_storage_local', 'extension/transport/chrome-adapter-core.mjs', 'agent_installation_id', REGISTERED_OBJECT, ['EXT-BROWSER-BINDING-STATE'], 'Current installation identity persisted by the Chrome adapter.'),
  declared('chrome-local:ofca_full_storage_bootstrap_v1', 'chrome_storage_local', 'extension/transport/chrome-adapter-core.mjs', 'ofca_full_storage_bootstrap_v1', REGISTERED_OBJECT, ['EXT-BROWSER-BINDING-STATE'], 'Current sealed Full-storage bootstrap.'),
  declared('chrome-session:active_account_partition_v5', 'chrome_storage_session', 'extension/transport/chrome-adapter-core.mjs', 'active_account_partition_v5', REGISTERED_OBJECT, ['EXT-BROWSER-BINDING-STATE'], 'Current session-selected encrypted account partition.'),
  declared('chrome-local:ofca_consent_v1', 'chrome_storage_local', 'extension/runtime/consent-controller.mjs', 'ofca_consent_v1', REGISTERED_OBJECT, ['EXT-BROWSER-CONTROL-STATE'], 'Current consent-mode control state.'),
  declared('chrome-local:ofca_legal_activation_flow_v1', 'chrome_storage_local', 'extension/runtime/legal-activation-controller.mjs', 'ofca_legal_activation_flow_v1', REGISTERED_OBJECT, ['EXT-BROWSER-CONTROL-STATE'], 'Current Legal activation workflow cursor.'),
  declared('idb:ofca_legal_evidence_v1:records', 'indexeddb_object_store', 'extension/runtime/activation-evidence.mjs', 'ofca_legal_evidence_v1', REGISTERED_OBJECT, ['EXT-LEGAL-EVIDENCE'], 'Append-only Legal activation evidence database.'),
  declared('chrome-local:ofca_preview_metrics_v1', 'chrome_storage_local', 'extension/runtime/preview-metrics.mjs', 'ofca_preview_metrics_v1', OUTSIDE_RET001_SCOPE, [], 'Preview-only aggregate metrics are outside the Full-mode/dataset RET-001 object boundary.'),
  declared('chrome-legacy:brain_binding_v2', 'chrome_storage_legacy_removal_only', 'extension/transport/chrome-adapter-core.mjs', 'brain_binding_v2', OUTSIDE_RET001_SCOPE, [], 'Legacy removal-only key; current Product does not write it.'),
  declared('chrome-legacy:agent_reconnect_auth_v2', 'chrome_storage_legacy_removal_only', 'extension/transport/chrome-adapter-core.mjs', 'agent_reconnect_auth_v2', OUTSIDE_RET001_SCOPE, [], 'Legacy removal-only key; current Product does not write it.'),
  declared('chrome-legacy:active_account_partition_v4', 'chrome_storage_legacy_removal_only', 'extension/transport/chrome-adapter-core.mjs', 'active_account_partition_v4', OUTSIDE_RET001_SCOPE, [], 'Legacy removal-only key; current Product does not write it.'),

  declared('sqlite:canonical.sqlite3:wal-shm', 'sqlite_physical_sidecars', 'app/persistence/database.py', 'PRAGMA journal_mode = WAL', REGISTERED_OBJECT, ['COMP-CANONICAL-WAL-SHM'], 'Canonical WAL/SHM is separately registered.'),
  declared('sqlite:auth.sqlite3:wal-shm', 'sqlite_physical_sidecars', 'app/persistence/database.py', 'PRAGMA journal_mode = WAL', COVERED_BY_EXISTING_OBJECT, ['COMP-AUTH-DATASET-STATE'], 'Auth WAL/SHM belongs to the encrypted auth.sqlite3 transactional/recovery boundary.'),
  declared('sqlite:projections.sqlite3:wal-shm', 'sqlite_physical_sidecars', 'app/persistence/database.py', 'PRAGMA journal_mode = WAL', COVERED_BY_EXISTING_OBJECT, ['COMP-BRIDGE-DERIVED-STATE', 'COMP-BRIDGE-PROJECTION-MESSAGES', 'COMP-BRIDGE-PROJECTION-STATE'], 'Projection WAL/SHM belongs to the projections.sqlite3 transactional/recovery boundary.'),
  declared('sqlite:analytics-projections.sqlite3:wal-shm', 'sqlite_physical_sidecars', 'app/persistence/database.py', 'PRAGMA journal_mode = WAL', COVERED_BY_EXISTING_OBJECT, ['COMP-ANALYTICS-GRAPH', 'COMP-ANALYTICS-PROJECTION', 'COMP-ANALYTICS-PROJECTION-STATE'], 'Analytics WAL/SHM belongs to the analytics-projections.sqlite3 transactional/recovery boundary.'),

  declared('backup:canonical:file', 'ordinary_backup', 'app/persistence/backup.py', 'backup_canonical_database', REGISTERED_OBJECT, ['COMP-BACKUP-CANONICAL'], 'Verified encrypted canonical backup.'),
  declared('backup:projections:file', 'ordinary_backup', 'app/persistence/backup.py', 'backup_projections_database', REGISTERED_OBJECT, ['COMP-BACKUP-PROJECTIONS'], 'Verified encrypted projections backup.'),
  declared('backup:external-manifest', 'backup_sidecar', 'app/persistence/backup.py', '_write_external_manifest', COVERED_BY_EXISTING_OBJECT, ['COMP-BACKUP-CANONICAL', 'COMP-BACKUP-PROJECTIONS'], 'Backup manifest publication is part of both ordinary backup objects.'),
  declared('backup:protected-key-sidecar', 'backup_sidecar', 'app/persistence/backup.py', '.key.dpapi', COVERED_BY_EXISTING_OBJECT, ['COMP-BACKUP-CANONICAL', 'COMP-BACKUP-PROJECTIONS'], 'Protected per-backup key sidecar is part of ordinary backup recoverability.'),
  declared('backup:publication-temporary', 'backup_temporary', 'app/persistence/backup.py', 'backup_temporary', COVERED_BY_EXISTING_OBJECT, ['COMP-BACKUP-CANONICAL', 'COMP-BACKUP-PROJECTIONS'], 'Temporary backup/manifest/key publication artifacts are cleaned on normal failure/success paths but belong to the backup objects while present.'),
  declared('migration:backup:file', 'migration_backup', 'app/persistence/migrations.py', '.bak', REGISTERED_OBJECT, ['COMP-MIGRATION-BACKUP'], 'Pre-migration encrypted SQLite copy.'),
  declared('migration:backup:temporary', 'migration_backup_temporary', 'app/persistence/migrations.py', '.tmp', COVERED_BY_EXISTING_OBJECT, ['COMP-MIGRATION-BACKUP'], 'Pre-publication migration backup temporary file.'),
  declared('restore:database:temporary', 'restore_temporary', 'app/persistence/backup.py', '.restore.tmp', REGISTERED_OBJECT, ['COMP-RESTORE-TEMP'], 'Verified restore staging database.'),
  declared('key:.ofca-master-key.dpapi', 'protected_key_material', 'app/security/local_data_key.py', '.ofca-master-key.dpapi', REGISTERED_OBJECT, ['COMP-LOCAL-KEY-MATERIAL'], 'DPAPI-protected installation master key.'),
  declared('migration:installation-lock', 'coordination_file', 'app/persistence/migrations.py', '.bridge-installation-migration.lock', OUTSIDE_RET001_SCOPE, [], 'Migration lock stores coordination state only and is not a dataset/content recoverability surface.'),

  declared('boundary:browser-indexeddb-physical-remnants', 'lower_level_forensic', null, null, UNVERIFIED_BELOW_SUPPORTED_BOUNDARY, [], 'Browser-engine physical remnants after IndexedDB logical deletion are below the supported application/browser API boundary.'),
  declared('boundary:browser-chrome-storage-physical-remnants', 'lower_level_forensic', null, null, UNVERIFIED_BELOW_SUPPORTED_BOUNDARY, [], 'Physical Chrome-storage remnants after clear/remove are below the supported application/browser API boundary.'),
  declared('boundary:filesystem-deleted-blocks-journal-cow', 'lower_level_forensic', null, null, UNVERIFIED_BELOW_SUPPORTED_BOUNDARY, [], 'Deleted filesystem blocks, filesystem journals, snapshots, and copy-on-write replicas are below the supported Product boundary.'),
  declared('boundary:process-memory-pagefile-swap-crash-dumps', 'lower_level_forensic', null, null, UNVERIFIED_BELOW_SUPPORTED_BOUNDARY, [], 'Process memory, pagefile/swap, and crash-dump recoverability are below the supported Product boundary.'),
  declared('boundary:windows-dpapi-forensic-internals', 'lower_level_forensic', null, null, UNVERIFIED_BELOW_SUPPORTED_BOUNDARY, [], 'Windows DPAPI credential/forensic internals are below the supported Product boundary.'),
]);

function normalize(value) {
  return String(value).toLowerCase().replace(/[^a-z0-9_]+/g, ' ');
}

function tableNames(sql) {
  return [...sql.matchAll(/\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_]+)/g)]
    .map((match) => match[1]);
}

function objectCoversSurface(entry, surface) {
  const physical = normalize(entry.location.physical);
  const logical = normalize(entry.location.logical);
  const expectedStore = normalize(surface.store);
  if (!physical.includes(expectedStore)) return false;
  return logical.split(/\s+/).includes(surface.name.toLowerCase());
}

function registryMatches(registry, surface) {
  const allIds = new Set(registry.objects.map((entry) => entry.id));
  const explicit = [
    ...(RET001_AGGREGATE_SURFACE_OBJECTS[surface.id] ?? []),
    ...(surface.store === 'auth.sqlite3' ? ['COMP-AUTH-DATASET-STATE'] : []),
  ];
  for (const objectId of explicit) {
    if (!allIds.has(objectId)) throw new Error(`RET-001 source-surface mapping references unknown registry object ${objectId}`);
  }
  const inferred = registry.objects
    .filter((entry) => objectCoversSurface(entry, surface))
    .map((entry) => entry.id);
  return [...new Set([...explicit, ...inferred])].sort();
}

function coverageBasis(surface, objectIds, registry) {
  if (objectIds.length === 0) return 'unmapped';
  if (surface.store === 'auth.sqlite3') return 'explicit_auth_store_mapping';
  if (RET001_AGGREGATE_SURFACE_OBJECTS[surface.id]?.length > 0) return 'explicit_aggregate_mapping';
  return registry.objects.some((entry) => objectCoversSurface(entry, surface))
    ? 'location_match'
    : 'explicit_mapping';
}

async function sqlSurfaces() {
  const surfaces = [];
  for (const definition of RET001_SQL_SURFACES) {
    const sql = await readFile(new URL(definition.source, ROOT), 'utf8');
    for (const name of tableNames(sql)) {
      surfaces.push(Object.freeze({
        id: `sql:${definition.store}:${name}`,
        kind: 'sqlite_table',
        store: definition.store,
        name,
        source: definition.source,
        source_token: `CREATE TABLE ${name}`,
      }));
    }
  }
  return surfaces;
}

function extensionIndexedDbSurfaces() {
  return [...Object.values(INGESTION_STORES), ENCRYPTION_KEY_CHECK_STORE].sort().map((name) => Object.freeze({
    id: `idb:full-account:${name}`,
    kind: 'indexeddb_object_store',
    store: 'onlyfans-agent-encrypted-account-v1',
    name,
    source: name === ENCRYPTION_KEY_CHECK_STORE
      ? 'extension/transport/encrypted-indexeddb-storage.mjs'
      : 'extension/transport/indexeddb-ingestion-storage.mjs',
    source_token: name,
  }));
}

async function verifyDeclaredSurfaceSource(surface) {
  if (surface.source === null) return surface;
  const text = await readFile(new URL(surface.source, ROOT), 'utf8');
  if (!text.includes(surface.source_token)) {
    throw new Error(`RET-001 declared source surface ${surface.id} token is absent from ${surface.source}`);
  }
  return surface;
}

export async function discoverRet001ApplicationSurfaces() {
  const declared = await Promise.all(RET001_DECLARED_NON_SCHEMA_SURFACES.map(verifyDeclaredSurfaceSource));
  const surfaces = [...extensionIndexedDbSurfaces(), ...await sqlSurfaces(), ...declared];
  surfaces.sort((left, right) => left.id.localeCompare(right.id));
  return surfaces;
}

export async function buildRet001SourceSurfaceAudit(registry = null) {
  const activeRegistry = registry ?? (await loadRet001Registry()).registry;
  const objectIds = new Set(activeRegistry.objects.map((entry) => entry.id));
  const surfaces = await discoverRet001ApplicationSurfaces();
  const audited = surfaces.map((surface) => {
    if (surface.disposition !== undefined) {
      for (const objectId of surface.registry_object_ids) {
        if (!objectIds.has(objectId)) throw new Error(`RET-001 source-surface mapping references unknown registry object ${objectId}`);
      }
      return Object.freeze({ ...surface, coverage_basis: 'explicit_disposition', status: surface.disposition });
    }
    const mapped = registryMatches(activeRegistry, surface);
    return Object.freeze({
      ...surface,
      registry_object_ids: mapped,
      coverage_basis: coverageBasis(surface, mapped, activeRegistry),
      disposition: mapped.length === 0 ? UNEXPLAINED : REGISTERED_OBJECT,
      reason: mapped.length === 0 ? 'No canonical registry object covers this discovered application surface.' : 'Discovered application schema surface is covered by the canonical registry.',
      status: mapped.length === 0 ? UNEXPLAINED : REGISTERED_OBJECT,
    });
  });
  const unexplained = audited.filter((surface) => surface.status === UNEXPLAINED);
  const counts = Object.fromEntries([...RET001_SOURCE_SURFACE_DISPOSITIONS, UNEXPLAINED].map((status) => [status, audited.filter((s) => s.status === status).length]));
  return Object.freeze({
    schema: 'ofca-ret-001-source-surface-audit/v2',
    product_baseline_revision: activeRegistry.product_baseline_revision,
    inventory_status: activeRegistry.inventory_status,
    supported_boundary: 'Product application code plus browser persistence APIs and Product-managed local files; lower-level browser/OS forensic recoverability is explicitly unverified.',
    surface_count: audited.length,
    registered_surface_count: audited.length - unexplained.length,
    unmapped_surface_count: unexplained.length,
    disposition_counts: Object.freeze(counts),
    surfaces: Object.freeze(audited),
    unmapped_surface_ids: Object.freeze(unexplained.map((surface) => surface.id)),
    outside_scope_surface_ids: Object.freeze(audited.filter((surface) => surface.status === OUTSIDE_RET001_SCOPE).map((surface) => surface.id)),
    unverified_surface_ids: Object.freeze(audited.filter((surface) => surface.status === UNVERIFIED_BELOW_SUPPORTED_BOUNDARY).map((surface) => surface.id)),
    completeness_gate: Object.freeze({
      enforced: activeRegistry.inventory_status === 'COMPLETE',
      status: activeRegistry.inventory_status === 'COMPLETE'
        ? (unexplained.length === 0 ? 'PASS' : 'FAIL')
        : 'DEFERRED_UNTIL_INVENTORY_COMPLETE',
    }),
  });
}

export function assertRet001SourceSurfaceCompleteness(audit) {
  if (audit.inventory_status !== 'COMPLETE') return audit;
  if (audit.unmapped_surface_count !== 0) {
    throw new Error(`RET-001 source-surface completeness failed: ${audit.unmapped_surface_ids.join(', ')}`);
  }
  if (audit.completeness_gate.status !== 'PASS') throw new Error('RET-001 source-surface completeness failed: gate did not pass');
  return audit;
}
