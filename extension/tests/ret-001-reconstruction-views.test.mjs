import assert from 'node:assert/strict';
import test from 'node:test';

import { loadRet001Registry } from '../../tools/retention-evidence/ret-001-registry.mjs';
import {
  analyzeRet001Deletion,
  buildRet001LegalMatrix,
  buildRet001ReconstructionGraph,
  buildRet001ResurrectionIndex,
  canonicalGeneratedJson,
  renderRet001LegalMatrixMarkdown,
} from '../../tools/retention-evidence/ret-001-views.mjs';

test('RET-001 Legal matrix and reconstruction graph are deterministic registry projections', async () => {
  const { registry } = await loadRet001Registry();
  const matrix = buildRet001LegalMatrix(registry);
  const graph = buildRet001ReconstructionGraph(registry);
  const index = buildRet001ResurrectionIndex(registry);
  assert.equal(matrix.rows.length, registry.objects.length);
  assert.equal(graph.nodes.length, registry.objects.length);
  assert.deepEqual(graph.nodes.map((node) => node.id), registry.objects.map((entry) => entry.id));
  assert.deepEqual(Object.keys(index.analyses), registry.objects.map((entry) => entry.id));
  for (const value of [matrix, graph, index]) {
    const first = canonicalGeneratedJson(value);
    const second = canonicalGeneratedJson(JSON.parse(first));
    assert.equal(second, first);
  }
  const markdown = renderRet001LegalMatrixMarkdown(registry);
  assert.match(markdown, /^# RET-001 Product object matrix/m);
  assert.match(markdown, /Inventory status: `COMPLETE`/);
  assert.match(markdown, /`COMP-CANONICAL-MESSAGES`/);
  assert.match(markdown, /authoritative_source=/);
  assert.match(markdown, /rebuild_or_replay_source=/);
});

test('RET-001 graph edges carry evidence provenance from the canonical registry', async () => {
  const { registry } = await loadRet001Registry();
  const graph = buildRet001ReconstructionGraph(registry);
  assert.ok(graph.edges.length > 0);
  for (const edge of graph.edges) {
    assert.match(edge.from, /^(?:EXT|COMP)-/);
    assert.match(edge.to, /^(?:EXT|COMP)-/);
    assert.ok(edge.evidence_ids.length > 0);
    assert.ok(edge.source_references.length > 0);
  }
});

test('RET-001 deletion analysis mechanically exposes surviving re-entry paths into canonical messages', async () => {
  const { registry } = await loadRet001Registry();
  const analysis = analyzeRet001Deletion(registry, ['COMP-CANONICAL-MESSAGES']);
  const target = analysis.targets[0];
  assert.equal(target.target_object_id, 'COMP-CANONICAL-MESSAGES');
  const directIds = new Set(target.direct_reentry_sources.map((source) => source.object_id));
  assert.ok(directIds.has('EXT-MESSAGES'));
  assert.ok(directIds.has('EXT-OUTBOX'));
  assert.ok(directIds.has('COMP-RAW-INGEST-EVENTS'));
  const upstreamIds = new Set(target.mechanical_reentry_sources.map((source) => source.object_id));
  assert.ok(upstreamIds.has('EXT-MESSAGES'));
  assert.ok(upstreamIds.has('EXT-OUTBOX'));
  const downstreamIds = new Set(target.surviving_downstream_copies_or_derivatives.map((source) => source.object_id));
  assert.ok(downstreamIds.has('COMP-BACKUP-CANONICAL'));
  assert.ok(downstreamIds.has('COMP-BRIDGE-PROJECTION-MESSAGES'));
});

test('RET-001 deletion analysis distinguishes a surviving downstream copy from executable reverse restoration', async () => {
  const { registry } = await loadRet001Registry();
  const analysis = analyzeRet001Deletion(registry, ['EXT-MESSAGES']);
  const target = analysis.targets[0];
  const downstream = new Map(target.surviving_downstream_copies_or_derivatives.map((entry) => [entry.object_id, entry]));
  assert.ok(downstream.has('COMP-CANONICAL-MESSAGES'));
  assert.ok(downstream.has('COMP-BACKUP-CANONICAL'));
  assert.equal(target.direct_reentry_sources.some((entry) => entry.object_id === 'COMP-CANONICAL-MESSAGES'), false, 'downstream copies must not be treated as reverse replay paths unless the registry declares one');
});

test('RET-001 reconstruction analysis supports deletion sets, not only one object', async () => {
  const { registry } = await loadRet001Registry();
  const analysis = analyzeRet001Deletion(registry, ['COMP-CANONICAL-MESSAGES','COMP-RAW-INGEST-EVENTS']);
  assert.deepEqual(analysis.deleted_object_ids, ['COMP-CANONICAL-MESSAGES','COMP-RAW-INGEST-EVENTS']);
  const canonical = analysis.targets.find((entry) => entry.target_object_id === 'COMP-CANONICAL-MESSAGES');
  assert.ok(canonical);
  assert.equal(canonical.direct_reentry_sources.some((entry) => entry.object_id === 'COMP-RAW-INGEST-EVENTS'), false, 'objects deleted in the same transaction model must not be counted as surviving re-entry sources');
  assert.ok(canonical.direct_reentry_sources.some((entry) => entry.object_id === 'EXT-OUTBOX'));
});
