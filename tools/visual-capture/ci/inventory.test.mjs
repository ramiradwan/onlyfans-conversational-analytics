import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { generateInventory } from './inventory.mjs';
import { CaptureRecorder, parseCaptureOptions, sourceIdentity } from './recording.mjs';

const source = { source_commit: 'a'.repeat(40), workflow_run_id: 'local', run_attempt: 1 };
const inventory = generateInventory();

test('independent inventory keeps whole page sequences and covers both groups without overlap', () => {
  const dynamic = inventory.cases.filter(value => value.group === 'dynamic');
  const remaining = inventory.cases.filter(value => value.group === 'remaining');
  assert.equal(new Set([...dynamic, ...remaining].map(value => value.id)).size, inventory.cases.length);
  assert.deepEqual(new Set(dynamic.map(value => value.kind)), new Set(['dynamic']));
  assert.deepEqual(new Set(remaining.map(value => value.kind)), new Set(['ordinary', 'freshness', 'review', 'static']));
  const home = dynamic.find(value => value.configuration.view === 'home' && value.configuration.width === 390);
  assert.equal(home.observations.filter(value => value === 'snapshot:fresh').length, 2);
  assert(home.observations.includes('protocol:unauthorized:false'));
  const freshness = remaining.find(value => value.kind === 'freshness');
  const edges = freshness.observations.filter(value => value.startsWith('edge:'));
  const overlays = freshness.observations.filter(value => value.startsWith('overlay:'));
  assert.equal(new Set(edges).size, 342); assert.equal(new Set(overlays).size, 26);
  for (const edge of edges) {
    const [, from, to] = edge.split(':');
    assert(edges.includes(`edge:${to}:${from}`));
  }
});

test('group mode refuses partial filters while bare local reproduction remains available', () => {
  for (const group of ['all', 'dynamic', 'remaining']) for (const variable of ['VISUAL_CAPTURE_ONLY', 'STATIC_SURFACE_ONLY']) {
    assert.throws(() => parseCaptureOptions(['out', '--stage-group', group], { [variable]: 'home' }), /partial_filter_refused/);
  }
  assert.equal(parseCaptureOptions(['out'], { VISUAL_CAPTURE_ONLY: 'home' }).partial, true);
  assert.throws(() => parseCaptureOptions(['out', '--stage-group', 'unknown'], {}));
  assert.throws(() => parseCaptureOptions(['out', '--workers', '8'], {}));
});

test('equal counts cannot substitute for exact transitions including duplicates and order', () => {
  const definition = inventory.cases.find(value => value.kind === 'dynamic' && value.configuration.view === 'home');
  for (const alter of [
    values => { [values[0], values[1]] = [values[1], values[0]]; },
    values => { values[values.indexOf('snapshot:fresh')] = 'snapshot:populated'; },
  ]) {
    const recorder = new CaptureRecorder({ group: 'dynamic', source });
    const finish = recorder.begin(definition.id, definition.configuration);
    const observations = [...definition.observations]; alter(observations);
    finish({ outcome: 'passed', observations, files: definition.files });
    assert.equal(recorder.records.get(definition.id).outcome, 'failed');
  }
});

test('only observed starts and terminals enter restricted metadata; private extras never leave it', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'visual-ci-recording-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  let now = 0;
  const recorder = new CaptureRecorder({ group: 'dynamic', source, clock: () => now });
  const [first, second] = inventory.cases.filter(value => value.group === 'dynamic');
  const finish = recorder.begin(first.id, first.configuration);
  finish({ outcome: 'passed', observations: [...first.observations, 'private-sentinel'], files: ['private-sentinel.txt'] });
  recorder.begin(second.id, second.configuration);
  assert.throws(() => recorder.begin('private-sentinel', {}), /invalid_case_start/);
  assert.throws(() => recorder.begin(first.id, first.configuration), /invalid_case_start/);
  assert.throws(() => finish({}), /duplicate_case_terminal/);
  now = 60_000;
  assert.match(recorder.progress('dynamic'), /elapsed_seconds=60 selected=216 completed=1$/);
  await recorder.writeInventory(directory);
  const receipt = await recorder.finish(directory, { exitCode: 1, phaseTimings: { dynamic: 60 } });
  assert.equal(receipt.complete, false); assert.equal(receipt.cases.length, 2);
  assert.deepEqual(new Set(receipt.cases.map(value => value.outcome)), new Set(['failed', 'incomplete']));
  assert.doesNotMatch(await readFile(join(directory, 'capture-ci.json'), 'utf8'), /private-sentinel/);
  assert.throws(() => recorder.progress('private-sentinel'), /invalid_phase/);
});

test('receipt provenance must resolve actual HEAD even with an expected revision', () => {
  let reads = 0;
  const head = () => { reads += 1; return source.source_commit; };
  assert.equal(sourceIdentity({ PRODUCT_SHA: source.source_commit }, '.', head).source_commit, source.source_commit);
  assert.throws(() => sourceIdentity({ PRODUCT_SHA: 'b'.repeat(40) }, '.', head), /invalid_provenance/);
  assert.equal(reads, 2);
});
