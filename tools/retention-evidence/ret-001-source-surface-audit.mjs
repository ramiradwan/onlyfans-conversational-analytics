import { readFile } from 'node:fs/promises';
import {
  RET001_AGGREGATE_SURFACE_OBJECTS,
  RET001_SOURCE_SURFACE_DISPOSITIONS,
  buildRet001SourceSurfaceAudit as buildSeedAudit,
  discoverRet001ApplicationSurfaces as discoverSeedSurfaces,
  assertRet001SourceSurfaceCompleteness as assertSeedCompleteness,
} from './ret-001-source-surface-audit.seed.mjs';

export { RET001_AGGREGATE_SURFACE_OBJECTS, RET001_SOURCE_SURFACE_DISPOSITIONS };

const TEST_KEY_SOURCE = 'app/security/local_data_key.py';
const TEST_KEY_SURFACE = Object.freeze({
  id: 'test-only:OFCA_TEST_DATABASE_MASTER_KEY_HEX',
  kind: 'test_only_environment_key',
  source: TEST_KEY_SOURCE,
  source_token: 'OFCA_TEST_DATABASE_MASTER_KEY_HEX',
  disposition: 'OUTSIDE_RET001_SCOPE',
  registry_object_ids: Object.freeze([]),
  reason: 'Test-only database-key injection is rejected outside tests and is not a durable Product persistence surface.',
});

async function verifiedTestKeySurface() {
  const text = await readFile(new URL(`../../${TEST_KEY_SOURCE}`, import.meta.url), 'utf8');
  if (!text.includes(TEST_KEY_SURFACE.source_token)) {
    throw new Error(`RET-001 declared source surface ${TEST_KEY_SURFACE.id} token is absent from ${TEST_KEY_SOURCE}`);
  }
  return TEST_KEY_SURFACE;
}

export async function discoverRet001ApplicationSurfaces() {
  const surfaces = [...await discoverSeedSurfaces(), await verifiedTestKeySurface()];
  surfaces.sort((left, right) => left.id.localeCompare(right.id));
  return Object.freeze(surfaces);
}

export async function buildRet001SourceSurfaceAudit(registry = null) {
  const seed = await buildSeedAudit(registry);
  const extra = await verifiedTestKeySurface();
  const surfaces = [...seed.surfaces, Object.freeze({ ...extra, coverage_basis: 'explicit_disposition', status: extra.disposition })]
    .sort((left, right) => left.id.localeCompare(right.id));
  const counts = { ...seed.disposition_counts, OUTSIDE_RET001_SCOPE: seed.disposition_counts.OUTSIDE_RET001_SCOPE + 1 };
  return Object.freeze({
    ...seed,
    surface_count: seed.surface_count + 1,
    registered_surface_count: seed.registered_surface_count + 1,
    disposition_counts: Object.freeze(counts),
    surfaces: Object.freeze(surfaces),
    outside_scope_surface_ids: Object.freeze([...seed.outside_scope_surface_ids, extra.id].sort()),
  });
}

export function assertRet001SourceSurfaceCompleteness(audit) {
  return assertSeedCompleteness(audit);
}
