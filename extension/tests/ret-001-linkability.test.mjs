import assert from 'node:assert/strict';
import test from 'node:test';

import { loadRet001Registry } from '../../tools/retention-evidence/ret-001-registry.mjs';
import {
  assertRet001LinkabilityCompleteness,
  buildRet001LinkabilityAudit,
  loadRet001Linkability,
  validateRet001Linkability,
} from '../../tools/retention-evidence/ret-001-linkability.mjs';

function byId(classification) {
  return new Map(classification.classifications.map((entry) => [entry.object_id, entry]));
}

test('RET-001 linkability classifies every current projection/analytics object exactly once', async () => {
  const { registry } = await loadRet001Registry();
  const { classification } = await loadRet001Linkability(registry);
  const derivedIds = registry.objects
    .filter((entry) => ['analytics-projections.sqlite3', 'projections.sqlite3'].includes(entry.location.physical))
    .map((entry) => entry.id)
    .sort();
  assert.deepEqual(classification.classifications.map((entry) => entry.object_id), derivedIds);
  assert.equal(classification.classification_status, 'COMPLETE');
});

test('RET-001 Bridge derived-state raw-text classification matches the completed canonical registry', async () => {
  const { registry } = await loadRet001Registry();
  const { classification } = await loadRet001Linkability(registry);
  const audit = buildRet001LinkabilityAudit(classification, registry);
  assert.deepEqual(audit.mismatch_object_ids, []);
  assert.deepEqual(audit.pending_object_ids, []);
  assert.equal(audit.completeness_gate, 'PASS');
  const bridge = byId(classification).get('COMP-BRIDGE-DERIVED-STATE');
  const registryBridge = registry.objects.find((entry) => entry.id === 'COMP-BRIDGE-DERIVED-STATE');
  assert.equal(bridge.raw_text_presence.status, 'PRESENT_SUBSET');
  assert.equal(registryBridge.raw_text_requirement.contains_raw_message_text, true);
  assert.equal(bridge.registry_consistency.status, 'MATCH');
  assert.equal(bridge.inference_or_reconstruction_capability.can_reconstruct_raw_text, 'SUBSET_ONLY');
  assert.equal(classification.scope.legal_policy_selected, false);
  assert.equal(classification.scope.production_lifecycle_changes_authorized, false);
  assert.doesNotThrow(() => assertRet001LinkabilityCompleteness(classification, registry));
});

test('RET-001 mature analytics are raw-text-free but remain linkable semantic derivatives', async () => {
  const { registry } = await loadRet001Registry();
  const { classification } = await loadRet001Linkability(registry);
  const entries = byId(classification);
  for (const objectId of ['COMP-ANALYTICS-GRAPH', 'COMP-ANALYTICS-PROJECTION']) {
    const entry = entries.get(objectId);
    assert.equal(entry.raw_text_presence.status, 'ABSENT');
    assert.equal(entry.unique_participant_semantics.status, 'PRESENT');
    assert.equal(entry.inference_or_reconstruction_capability.can_reconstruct_raw_text, false);
    assert.equal(entry.inference_or_reconstruction_capability.can_strongly_infer_source_information, true);
    assert.notEqual(entry.canonical_joinability.status, 'NONE');
    assert.equal(entry.usefulness_after_identifier_removal.status, 'RETAINS_REDUCED_UTILITY');
  }
});

test('RET-001 Bridge projection messages remain a direct raw-content copy', async () => {
  const { registry } = await loadRet001Registry();
  const { classification } = await loadRet001Linkability(registry);
  const messages = byId(classification).get('COMP-BRIDGE-PROJECTION-MESSAGES');
  assert.equal(messages.raw_text_presence.status, 'PRESENT');
  assert.equal(messages.identifiers.message.status, 'DIRECT');
  assert.equal(messages.identifiers.conversation.status, 'DIRECT');
  assert.equal(messages.identifiers.participant.status, 'ABSENT');
  assert.equal(messages.canonical_joinability.status, 'DIRECT_STABLE_IDENTIFIER');
  assert.equal(messages.inference_or_reconstruction_capability.classification, 'RAW_CONTENT_COPY');
  assert.equal(messages.inference_or_reconstruction_capability.can_reconstruct_raw_text, true);
});

test('RET-001 linkability rejects missing or invented derived-object classifications', async () => {
  const { registry } = await loadRet001Registry();
  const { classification } = await loadRet001Linkability(registry);
  const missing = structuredClone(classification);
  missing.classifications = missing.classifications.slice(1);
  assert.throws(() => validateRet001Linkability(missing, registry), /classification coverage differs from canonical derived objects/);
  const invented = structuredClone(classification);
  invented.classifications.push({ ...structuredClone(invented.classifications.at(-1)), object_id: 'COMP-NOT-REGISTERED' });
  invented.classifications.sort((left, right) => left.object_id.localeCompare(right.object_id));
  assert.throws(() => validateRet001Linkability(invented, registry), /classification coverage differs from canonical derived objects/);
});

test('RET-001 linkability rejects a hidden raw-text inconsistency', async () => {
  const { registry } = await loadRet001Registry();
  const { classification } = await loadRet001Linkability(registry);
  const mutated = structuredClone(classification);
  const bridge = mutated.classifications.find((entry) => entry.object_id === 'COMP-BRIDGE-DERIVED-STATE');
  bridge.raw_text_presence.status = 'ABSENT';
  bridge.registry_consistency = { status: 'MATCH', discrepancies: [] };
  assert.throws(() => validateRet001Linkability(mutated, registry), /registry_consistency must truthfully reflect raw-text classification mismatch/);
});
