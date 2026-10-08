import assert from 'node:assert/strict';
import test from 'node:test';
import { createOnboardingProjection } from './projection.mjs';
import { readFileSync } from 'node:fs';

const vectors = JSON.parse(readFileSync(new URL('./vectors.json', import.meta.url)));
const fixture = (id) => structuredClone(vectors.cases.find((v) => v.id === id).value);
const snapshot = fixture('extension-snapshot');
const command = fixture('extension-command');
const result = fixture('result-confirmed');
const start = () => {
  const projection = createOnboardingProjection(snapshot.journey_id);
  projection.channel('extension', snapshot.epoch);
  assert.equal(projection.receive('extension', snapshot), 'accepted');
  return projection;
};

test('a disconnect removes certainty, even while the browser page remains open', () => {
  const projection = start();
  projection.begin(command);
  projection.disconnect('extension');
  assert.equal(projection.read().sources.extension.certain, false);
  assert.equal(projection.read().operations[command.operation_id].status, 'unknown');
  assert.equal(projection.settle('extension', result), false);
});

test('revision gaps require a new snapshot and old epochs never replace it', () => {
  const projection = start();
  assert.equal(projection.receive('extension', { ...snapshot, kind: 'event', revision: 2 }), 'snapshot_required');
  assert.equal(projection.read().sources.extension.certain, false);
  assert.equal(projection.receive('extension', { ...snapshot, revision: 3 }), 'accepted');
  assert.equal(projection.receive('extension', { ...snapshot, epoch: command.operation_id, revision: 900 }), 'ignored');
  assert.equal(projection.receive('brain', { ...snapshot, revision: 4 }), 'ignored');
  assert.equal(projection.read().sources.extension.revision, 3);
});

test('the authoritative owner and matching committed scope settle an operation once', () => {
  const projection = start();
  assert.equal(projection.begin(command), true);
  assert.equal(projection.begin(command), true);
  assert.equal(projection.begin({ ...command, action: 'resume' }), false);
  assert.equal(projection.settle('extension', result), false);
  projection.receive('extension', { ...snapshot, revision: 1, kind: 'event' });
  assert.equal(projection.settle('brain', result), false);
  assert.equal(projection.settle('extension', { ...result, consent_generation: 1 }), false);
  assert.equal(projection.settle('extension', result), true);
  assert.equal(projection.settle('extension', { ...result, status: 'unknown' }), false);
});

test('creator or consent changes fence earlier commands and acknowledgements', () => {
  const projection = start();
  projection.begin(command);
  projection.receive('extension', { ...snapshot, revision: 1, kind: 'event', account_generation: 2, consent_generation: 3 });
  assert.equal(projection.begin({ ...command, operation_id: snapshot.epoch }), false);
  assert.equal(projection.settle('extension', result), false);
  assert.equal(projection.read().operations[command.operation_id].status, 'unknown');
});

test('new authenticated channel requires fresh snapshot and never combines epoch revisions', () => {
  const projection = start();
  projection.channel('extension', command.operation_id);
  assert.equal(projection.receive('extension', { ...snapshot, kind: 'event', revision: 1 }), 'ignored');
  assert.equal(projection.receive('extension', { ...snapshot, epoch: command.operation_id }), 'accepted');
  assert.equal(projection.read().sources.extension.revision, 0);
});

test('a completed action keeps its historical outcome after the creator changes', () => {
  const projection = start();
  projection.begin(command);
  projection.receive('extension', { ...snapshot, revision: 1, kind: 'event' });
  assert.equal(projection.settle('extension', result), true);
  projection.receive('extension', { ...snapshot, revision: 2, kind: 'event', account_generation: 2, consent_generation: 3 });
  assert.equal(projection.read().operations[command.operation_id].status, 'confirmed');
  assert.equal(projection.begin(command), false);
  assert.equal(projection.settle('extension', result), false);
});
