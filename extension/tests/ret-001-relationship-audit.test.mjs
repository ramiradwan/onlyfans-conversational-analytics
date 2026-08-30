import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadRet001Registry } from '../../tools/retention-evidence/ret-001-registry.mjs';
import {
  answerRet001ResurrectionQuestion,
  assertRet001RelationshipCompleteness,
  buildRet001RelationshipAudit,
  buildRet001ResurrectionAnswers,
} from '../../tools/retention-evidence/ret-001-relationship-audit.mjs';

test('RET-001 relationship audit tracks every registered object without claiming phase-1 completeness', async () => {
  const { registry } = await loadRet001Registry();
  const audit = buildRet001RelationshipAudit(registry);

  assert.equal(audit.summary.object_count, registry.objects.length);
  assert.equal(audit.inventory_status, registry.inventory_status);
  assert.equal(audit.completeness_gate.enforced, registry.inventory_status === 'COMPLETE');
  if (registry.inventory_status !== 'COMPLETE') {
    assert.equal(audit.completeness_gate.status, 'DEFERRED_UNTIL_INVENTORY_COMPLETE');
  }
});

test('RET-001 resurrection answers are generated for every object from the canonical registry', async () => {
  const { registry } = await loadRet001Registry();
  const generated = buildRet001ResurrectionAnswers(registry);

  assert.deepEqual(
    Object.keys(generated.answers),
    registry.objects.map((entry) => entry.id),
  );
  for (const entry of registry.objects) {
    const answer = generated.answers[entry.id];
    assert.equal(answer.deleted_object_id, entry.id);
    assert.match(answer.question, new RegExp(entry.id));
    for (const source of answer.declared_reentry_sources) {
      assert.match(source.path_kind, /^[A-Z_]+$/);
      assert.match(source.execution_evidence, /^[A-Z_]+$/);
      assert.ok(source.evidence_ids.length > 0);
    }
    if (registry.inventory_status !== 'COMPLETE') {
      assert.ok(answer.limitations.includes('REGISTRY_INVENTORY_INCOMPLETE'));
    }
  }
});

test('RET-001 canonical-message answer distinguishes declared re-entry from semantic survivors', async () => {
  const { registry } = await loadRet001Registry();
  const answer = answerRet001ResurrectionQuestion(registry, 'COMP-CANONICAL-MESSAGES');

  const declaredIds = new Set(answer.declared_reentry_sources.map((entry) => entry.object_id));
  assert.ok(declaredIds.has('EXT-MESSAGES'));
  assert.ok(declaredIds.has('EXT-OUTBOX'));

  const semanticIds = new Set(
    answer.surviving_equivalent_information_holders.map((entry) => entry.object_id),
  );
  assert.ok(semanticIds.has('COMP-BRIDGE-PROJECTION-MESSAGES'));

  assert.ok(
    answer.declared_reentry_sources.some(
      (entry) => entry.execution_evidence === 'EXECUTABLE_REENTRY_NOT_ESTABLISHED_BY_RELATIONSHIP_TYPE'
        || entry.execution_evidence === 'COPY_RELATIONSHIP_DECLARED'
        || entry.execution_evidence === 'REBUILD_OR_REPLAY_RELATIONSHIP_DECLARED'
        || entry.execution_evidence === 'EXECUTABLE_REENTRY_REQUIRES_SCENARIO_CHARACTERIZATION',
    ),
  );
});

test('RET-001 relationship completeness becomes fail-closed when an inventory is promoted to COMPLETE', async () => {
  const { registry } = await loadRet001Registry();
  const mutated = structuredClone(registry);
  mutated.inventory_status = 'COMPLETE';

  const isolatedId = 'EXT-MESSAGES';
  for (const entry of mutated.objects) {
    const reconstruction = entry.reconstruction;
    for (const field of [
      'authoritative_source_object_ids',
      'derived_from_object_ids',
      'copies_to_object_ids',
      'rebuild_or_replay_source_object_ids',
    ]) {
      reconstruction[field] = reconstruction[field].filter(
        (objectId) => objectId !== isolatedId,
      );
    }
  }

  const isolated = mutated.objects.find((entry) => entry.id === isolatedId);
  isolated.reconstruction.authoritative_source_object_ids = [];
  isolated.reconstruction.derived_from_object_ids = [];
  isolated.reconstruction.copies_to_object_ids = [];
  isolated.reconstruction.rebuild_or_replay_source_object_ids = [];
  isolated.reconstruction.can_recreate_deleted_content_or_identifying_semantics = true;

  assert.throws(
    () => assertRet001RelationshipCompleteness(mutated),
    /RET-001 relationship completeness failed/,
  );
});

test('RET-001 view generator packages relationship audit, resurrection answers and scenario bindings', async () => {
  const outputDir = await mkdtemp(join(tmpdir(), 'ret-001-views-'));
  try {
    execFileSync(
      process.execPath,
      [
        fileURLToPath(
          new URL('../../tools/retention-evidence/generate-ret-001-views.mjs', import.meta.url),
        ),
        '--output-dir',
        outputDir,
      ],
      { stdio: 'pipe' },
    );

    const manifest = JSON.parse(
      await readFile(join(outputDir, 'manifest.json'), 'utf8'),
    );
    const relationshipAudit = JSON.parse(
      await readFile(join(outputDir, 'relationship-audit.json'), 'utf8'),
    );
    const resurrectionAnswers = JSON.parse(
      await readFile(join(outputDir, 'resurrection-answers.json'), 'utf8'),
    );
    const scenarioCatalog = JSON.parse(
      await readFile(join(outputDir, 'resurrection-scenario-catalog.json'), 'utf8'),
    );
    const scenarioBindings = JSON.parse(
      await readFile(join(outputDir, 'resurrection-scenario-bindings.json'), 'utf8'),
    );

    assert.ok(manifest.generated_files['relationship-audit.json']);
    assert.ok(manifest.generated_files['resurrection-answers.json']);
    assert.ok(manifest.generated_files['resurrection-scenario-catalog.json']);
    assert.ok(manifest.generated_files['resurrection-scenario-bindings.json']);
    assert.equal(manifest.generated_files['resurrection-index.json'], undefined);
    assert.deepEqual(
      manifest.relationship_completeness_gate,
      relationshipAudit.completeness_gate,
    );
    assert.deepEqual(
      manifest.resurrection_scenario_binding_gate,
      scenarioBindings.binding_gate,
    );
    assert.equal(
      Object.keys(resurrectionAnswers.answers).length,
      relationshipAudit.summary.object_count,
    );
    assert.equal(
      scenarioCatalog.scenarios.length,
      scenarioBindings.summary.scenario_count,
    );
    if (manifest.inventory_status !== 'COMPLETE') {
      assert.equal(
        manifest.relationship_completeness_gate.status,
        'DEFERRED_UNTIL_INVENTORY_COMPLETE',
      );
      assert.equal(
        manifest.resurrection_scenario_binding_gate.status,
        'DEFERRED_UNTIL_INVENTORY_COMPLETE',
      );
    }
  } finally {
    await rm(outputDir, { recursive: true, force: true });
  }
});
