#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';

import { buildRet001ExecutedScenarioSummary, loadRet001ExecutedScenarioResults } from './ret-001-executed-scenarios.mjs';
import { assertRet001LinkabilityCompleteness, buildRet001LinkabilityAudit, loadRet001Linkability, renderRet001LinkabilityMarkdown } from './ret-001-linkability.mjs';
import { loadRet001Registry } from './ret-001-registry.mjs';
import { assertRet001RelationshipCompleteness, buildRet001RelationshipAudit, buildRet001ResurrectionAnswers } from './ret-001-relationship-audit.mjs';
import { assertRet001ScenarioBindingCompleteness, buildRet001ResurrectionScenarioBindings, buildRet001ResurrectionScenarioCatalog } from './ret-001-resurrection-scenarios.mjs';
import { assertRet001SourceSurfaceCompleteness, buildRet001SourceSurfaceAudit } from './ret-001-source-surface-audit.mjs';
import { buildRet001LegalMatrix, buildRet001ReconstructionGraph, canonicalGeneratedJson, renderRet001LegalMatrixMarkdown } from './ret-001-views.mjs';

function parseArgs(argv) {
  const result = { outputDir: null, productRevision: null, scenarioResultsDir: null };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === '--output-dir') { result.outputDir = argv[index + 1]; index += 1; continue; }
    if (item === '--product-revision') { result.productRevision = argv[index + 1]; index += 1; continue; }
    if (item === '--scenario-results-dir') { result.scenarioResultsDir = argv[index + 1]; index += 1; continue; }
    throw new Error(`Unknown RET-001 view generator argument: ${item}`);
  }
  if ((result.productRevision === null) !== (result.scenarioResultsDir === null)) {
    throw new Error('RET-001 executed evidence requires both --product-revision and --scenario-results-dir');
  }
  return result;
}
function sha256(text) { return createHash('sha256').update(text).digest('hex'); }

const args = parseArgs(process.argv.slice(2));
const { registry, text: registryText } = await loadRet001Registry();
const { classification: linkability, text: linkabilityText } = await loadRet001Linkability(registry);
const sourceSurfaceAudit = await buildRet001SourceSurfaceAudit(registry);
const relationshipAudit = buildRet001RelationshipAudit(registry);
const scenarioCatalog = buildRet001ResurrectionScenarioCatalog(registry);
const scenarioBindings = buildRet001ResurrectionScenarioBindings(registry);
const linkabilityAudit = buildRet001LinkabilityAudit(linkability, registry);
assertRet001SourceSurfaceCompleteness(sourceSurfaceAudit);
assertRet001RelationshipCompleteness(registry);
assertRet001ScenarioBindingCompleteness(registry);
assertRet001LinkabilityCompleteness(linkability, registry);

const executedResultFiles = args.scenarioResultsDir === null
  ? Object.freeze([])
  : await loadRet001ExecutedScenarioResults(registry, args.scenarioResultsDir, args.productRevision);
const executedScenarioSummary = args.scenarioResultsDir === null
  ? null
  : buildRet001ExecutedScenarioSummary(registry, executedResultFiles, args.productRevision);
if (
  registry.inventory_status === 'COMPLETE'
  && executedScenarioSummary !== null
  && executedScenarioSummary.execution_coverage_status !== 'COMPLETE'
) {
  throw new Error(
    `RET-001 COMPLETE inventory evidence requires complete executed scenario coverage; pending: ${executedScenarioSummary.pending_scenario_ids.join(', ')}`,
  );
}

const outputDir = resolve(args.outputDir ?? `artifacts/legal/ret-001/${args.productRevision ?? registry.product_baseline_revision}/phase-2-views`);
await mkdir(outputDir, { recursive: true });

const files = {
  'derived-linkability.json': canonicalGeneratedJson(linkability),
  'derived-linkability.md': renderRet001LinkabilityMarkdown(linkability, registry),
  'linkability-audit.json': canonicalGeneratedJson(linkabilityAudit),
  'object-matrix.json': canonicalGeneratedJson(buildRet001LegalMatrix(registry)),
  'object-matrix.md': renderRet001LegalMatrixMarkdown(registry),
  'reconstruction-graph.json': canonicalGeneratedJson(buildRet001ReconstructionGraph(registry)),
  'resurrection-answers.json': canonicalGeneratedJson(buildRet001ResurrectionAnswers(registry)),
  'relationship-audit.json': canonicalGeneratedJson(relationshipAudit),
  'resurrection-scenario-catalog.json': canonicalGeneratedJson(scenarioCatalog),
  'resurrection-scenario-bindings.json': canonicalGeneratedJson(scenarioBindings),
  'source-surface-audit.json': canonicalGeneratedJson(sourceSurfaceAudit),
};
if (executedScenarioSummary !== null) {
  files['executed-scenario-summary.json'] = canonicalGeneratedJson(executedScenarioSummary);
  for (const file of executedResultFiles) files[`scenario-results/${file.name}`] = file.text;
}
for (const [name, content] of Object.entries(files)) {
  const destination = resolve(outputDir, name);
  await mkdir(dirname(destination), { recursive: true });
  await writeFile(destination, content, 'utf8');
}

const manifest = {
  schema: 'ofca-ret-001-generated-views-manifest/v1',
  product_baseline_revision: registry.product_baseline_revision,
  product_execution_revision: args.productRevision,
  registry_version: registry.registry_version,
  inventory_status: registry.inventory_status,
  source_surface_completeness_gate: sourceSurfaceAudit.completeness_gate,
  source_surface_disposition_counts: sourceSurfaceAudit.disposition_counts,
  source_surface_unverified_ids: sourceSurfaceAudit.unverified_surface_ids,
  source_surface_outside_scope_ids: sourceSurfaceAudit.outside_scope_surface_ids,
  relationship_completeness_gate: relationshipAudit.completeness_gate,
  resurrection_scenario_binding_gate: scenarioBindings.binding_gate,
  linkability_classification_status: linkability.classification_status,
  linkability_completeness_gate: linkabilityAudit.completeness_gate,
  linkability_mismatch_object_ids: linkabilityAudit.mismatch_object_ids,
  executed_scenario_coverage: executedScenarioSummary === null ? null : {
    status: executedScenarioSummary.execution_coverage_status,
    executed_scenario_count: executedScenarioSummary.summary.executed_scenario_count,
    pending_scenario_count: executedScenarioSummary.summary.pending_scenario_count,
    executed_scenario_ids: executedScenarioSummary.executed_scenario_ids,
    pending_scenario_ids: executedScenarioSummary.pending_scenario_ids,
  },
  registry_sha256: sha256(registryText),
  linkability_sha256: sha256(linkabilityText),
  generated_files: Object.fromEntries(Object.entries(files).sort(([left], [right]) => left.localeCompare(right)).map(([name, content]) => [name, { sha256: sha256(content) }])),
};
await writeFile(resolve(outputDir, 'manifest.json'), canonicalGeneratedJson(manifest), 'utf8');

process.stdout.write(`${canonicalGeneratedJson({
  output_dir: outputDir,
  registry_sha256: manifest.registry_sha256,
  linkability_sha256: manifest.linkability_sha256,
  product_execution_revision: manifest.product_execution_revision,
  inventory_status: registry.inventory_status,
  source_surface_completeness_gate: sourceSurfaceAudit.completeness_gate,
  relationship_completeness_gate: relationshipAudit.completeness_gate,
  resurrection_scenario_binding_gate: scenarioBindings.binding_gate,
  linkability_completeness_gate: linkabilityAudit.completeness_gate,
  linkability_mismatch_object_ids: linkabilityAudit.mismatch_object_ids,
  executed_scenario_coverage: manifest.executed_scenario_coverage,
  generated_files: Object.keys(files).sort(),
})}`);
