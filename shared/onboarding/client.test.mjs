import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createOnboardingClient } from './client.mjs';

const vectors = JSON.parse(readFileSync(new URL('./vectors.json', import.meta.url)));
const fixture = (id) => structuredClone(vectors.cases.find((v) => v.id === id).value);
const state = fixture('extension-snapshot');
const command = fixture('extension-command');
const result = fixture('result-confirmed');
const tick = () => new Promise((resolve) => setImmediate(resolve));
function setup(options = {}) {
  let listener;
  let disconnected;
  let resolveRead;
  let reads = 0;
  let invalidations = 0;
  const sent = [];
  const adapter = {
    subscribe(next, close) { listener = next; disconnected = close; return () => {}; },
    readSnapshot() { reads += 1; return new Promise((resolve) => { resolveRead = resolve; }); },
    sendCommand(value) { sent.push(value); },
    invalidate() { invalidations += 1; },
  };
  // Wire shape is checked by the separately qualified canonical validator in
  // each runtime. These tests inject refusal to exercise the adapter boundary.
  const client = createOnboardingClient({ journeyId: state.journey_id,
    validate: (value) => typeof value === 'object' && value !== null && !value.invalid, ...options });
  const detach = client.attach('extension', adapter);
  return { client, adapter, sent, detach, receive: (value) => listener(value),
    disconnect: () => disconnected(), read: (value = state) => resolveRead(value),
    reads: () => reads, invalidations: () => invalidations };
}

test('subscription captures committed changes while initial snapshot is pending', async () => {
  const run = setup();
  run.receive({ ...state, kind: 'event', revision: 1, reason: 'paused' });
  run.read();
  await tick();
  assert.equal(run.client.getState().sources.extension.revision, 1);
  assert.equal(run.client.getState().sources.extension.snapshot.reason, 'paused');
  assert.equal(run.reads(), 1);
});

test('invalid, foreign journey and wrong owner data cannot become authoritative', async () => {
  const run = setup(); run.read(); await tick();
  for (const extra of [{ invalid: true }, { source: 'brain' }, { journey_id: command.operation_id }]) {
    run.receive({ ...state, kind: 'event', revision: 1, ...extra });
  }
  assert.equal(run.client.getState().sources.extension.revision, 0);
});

test('pending is published before send and only committed owner result confirms', async () => {
  const run = setup(); run.read(); await tick();
  run.adapter.sendCommand = () => {
    assert.equal(run.client.getState().operations[command.operation_id].status, 'pending');
  };
  await run.client.command(command);
  run.receive(result); // Result races state; keep it bounded until state commits.
  assert.equal(run.client.getState().operations[command.operation_id].status, 'pending');
  run.receive({ ...state, kind: 'event', revision: 1 });
  assert.equal(run.client.getState().operations[command.operation_id].status, 'confirmed');
});

test('lost delivery becomes unknown and repeating the same intent never replays it', async () => {
  const run = setup(); run.read(); await tick();
  let sends = 0;
  run.adapter.sendCommand = async () => { sends += 1; throw new Error('lost reply'); };
  assert.equal(await run.client.command(command), true);
  assert.equal(run.client.getState().operations[command.operation_id].status, 'unknown');
  assert.equal(await run.client.command(command), true);
  assert.equal(sends, 1);
});

test('disconnect removes readiness and fences late callbacks from the old channel', async () => {
  const run = setup(); run.read(); await tick();
  await run.client.command(command);
  run.disconnect();
  run.receive({ ...state, kind: 'event', revision: 1 });
  run.receive(result);
  assert.equal(run.client.getState().sources.extension.certain, false);
  assert.equal(run.client.getState().operations[command.operation_id].status, 'unknown');
});

test('a gap triggers one fresh read and buffers commits during recovery', async () => {
  const run = setup(); run.read(); await tick();
  run.receive({ ...state, kind: 'event', revision: 2 });
  assert.equal(run.client.getState().sources.extension.certain, false);
  run.receive({ ...state, kind: 'event', revision: 3 });
  run.read({ ...state, revision: 2 });
  await tick();
  assert.equal(run.client.getState().sources.extension.revision, 3);
  assert.equal(run.client.getState().sources.extension.certain, true);
  assert.equal(run.reads(), 2);
});

test('a continuing gap terminates reconciliation instead of creating status polling', async () => {
  const run = setup(); run.read(); await tick();
  run.receive({ ...state, kind: 'event', revision: 2 });
  run.receive({ ...state, kind: 'event', revision: 4 });
  run.read({ ...state, revision: 2 });
  await tick();
  assert.equal(run.client.getState().sources.extension.certain, false);
  assert.equal(run.reads(), 2);
  assert.equal(run.invalidations(), 1);
});

test('an event cannot establish a new source epoch', async () => {
  const run = setup(); run.read(); await tick();
  run.receive({ ...state, kind: 'event', revision: 1, epoch: command.operation_id });
  assert.equal(run.client.getState().sources.extension.certain, false);
  assert.equal(run.client.getState().sources.extension.epoch, state.epoch);
  assert.equal(run.invalidations(), 1);
});

test('buffer overflow fails closed without retaining unbounded messages', async () => {
  const run = setup({ bufferLimit: 2 });
  for (let revision = 1; revision <= 3; revision += 1) run.receive({ ...state, kind: 'event', revision });
  run.read(); await tick();
  assert.equal(run.client.getState().sources.extension, undefined);
  assert.equal(run.invalidations(), 1);
});

test('operation limit never silently drops unresolved operations', async () => {
  const run = setup({ operationLimit: 1 }); run.read(); await tick();
  assert.equal(await run.client.command(command), true);
  assert.equal(await run.client.command({ ...command, operation_id: state.epoch }), false);
  assert.equal(run.sent.length, 1);
  assert.equal(run.client.getState().operations[command.operation_id].status, 'pending');
});

test('generation-changing pause confirms only after its exact committed scope arrives', async () => {
  const run = setup(); run.read(); await tick();
  await run.client.command(command);
  const committed = fixture('result-v2-generation-changing-commit');
  run.receive(committed);
  assert.equal(run.client.getState().operations[command.operation_id].status, 'pending');
  run.receive({ ...state, kind: 'event', revision: 1 });
  run.receive({ ...state, kind: 'event', revision: 2, consent_generation: 3,
    facts: { ...state.facts, capture: 'paused' }, reason: 'paused' });
  assert.equal(run.client.getState().operations[command.operation_id].status, 'confirmed');
});

test('a changed creator cannot confirm a former creator command through v2', async () => {
  const run = setup(); run.read(); await tick();
  await run.client.command(command);
  run.receive({ ...state, revision: 2, account_generation: 2, consent_generation: 3 });
  run.receive({ ...fixture('result-v2-generation-changing-commit'), account_generation: 2 });
  assert.equal(run.client.getState().operations[command.operation_id].status, 'unknown');
});

test('v2 must identify the original consent generation as well as the committed generation', async () => {
  const run = setup(); run.read(); await tick();
  await run.client.command(command);
  run.receive({ ...state, revision: 2, consent_generation: 3 });
  run.receive({ ...fixture('result-v2-generation-changing-commit'), command_consent_generation: 1 });
  assert.equal(run.client.getState().operations[command.operation_id].status, 'unknown');
});

test('authenticated restart reads a receipt once without replaying a command or comparing epoch counters', async () => {
  const run = setup(); run.read(); await tick();
  await run.client.command(command);
  run.disconnect();
  const restarted = { ...state, epoch: command.operation_id, revision: 0,
    account_generation: 0, consent_generation: 0 };
  let receive;
  const reads = [];
  run.client.attach('extension', {
    subscribe(next) { receive = next; return () => {}; },
    async readSnapshot() { return restarted; },
    sendCommand() { assert.fail('receipt recovery must not replay'); },
    readOperation(id) {
      reads.push(id);
      receive({ ...fixture('result-v2-generation-changing-commit'), epoch: restarted.epoch,
        revision: 0, account_generation: 0, consent_generation: 0 });
    },
  });
  await tick();
  assert.deepEqual(reads, [command.operation_id]);
  assert.equal(run.client.getState().operations[command.operation_id].status, 'confirmed');
  assert.equal(run.sent.length, 1);
});

test('matching numeric generations after restart cannot replace the original command epoch', async () => {
  const run = setup(); run.read(); await tick(); await run.client.command(command); run.disconnect();
  let receive;
  run.client.attach('extension', {
    subscribe(next) { receive = next; return () => {}; },
    async readSnapshot() { return { ...state, epoch: command.operation_id, revision: 2, consent_generation: 3 }; },
    sendCommand() { assert.fail(); },
  });
  await tick();
  receive({ ...fixture('result-v2-generation-changing-commit'), epoch: command.operation_id,
    command_epoch: command.operation_id });
  assert.equal(run.client.getState().operations[command.operation_id].status, 'unknown');
});
