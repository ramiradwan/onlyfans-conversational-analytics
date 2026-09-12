import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import test from 'node:test';
import vm from 'node:vm';
import { createLineQueue, stopChild, waitForWorkerEntry } from '../process-lines.mjs';

test('child line queue preserves lines emitted before the second read', async () => {
  const child = spawn(process.execPath, ['-e', "process.stdout.write('ready\\napplication-secret-observed=false\\n'); setTimeout(() => {}, 1000)"], { stdio: ['ignore', 'pipe', 'inherit'] });
  const lines = createLineQueue(child, 'fixture');
  assert.equal(await lines.nextLine(1000), 'ready');
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(await lines.nextLine(1000), 'application-secret-observed=false');
  await stopChild(child, 'fixture');
});

test('worker discovery waits for module entry and refuses a missing entry at the deadline', async () => {
  const worker = { evaluate: (fn, args) => fn(args) };
  const entry = '__delayedQualificationTest';
  const timer = setTimeout(() => { globalThis[entry] = () => {}; }, 25);
  try { await waitForWorkerEntry(worker, entry, 200); }
  finally { clearTimeout(timer); delete globalThis[entry]; }
  await assert.rejects(waitForWorkerEntry(worker, entry, 20), /worker_initialization_timeout/u);
});

test('worker discovery needs no worker timers or clock before module startup', async () => {
  const sandbox = vm.createContext({});
  const worker = { evaluate: (fn, args) => {
    sandbox.entry = args;
    const value = vm.runInContext(`(${fn.toString()})(entry)`, sandbox);
    assert.equal(typeof value, 'boolean');
    return Promise.resolve(value);
  } };
  const timer = setTimeout(() => { sandbox.qualificationEntry = () => {}; }, 25);
  try { await waitForWorkerEntry(worker, 'qualificationEntry', 200); }
  finally { clearTimeout(timer); }
});

test('worker evaluation errors refuse immediately without retrying', async () => {
  let calls = 0;
  const failure = new Error('worker_context_unavailable');
  const worker = { evaluate: () => { calls += 1; throw failure; } };
  await assert.rejects(waitForWorkerEntry(worker, 'entry', 200), (error) => error === failure);
  assert.equal(calls, 1);
});

test('a stalled worker evaluation cannot extend the initialization deadline', async () => {
  let calls = 0;
  const worker = { evaluate: () => { calls += 1; return new Promise(() => {}); } };
  await assert.rejects(waitForWorkerEntry(worker, 'entry', 20), /worker_initialization_timeout/u);
  assert.equal(calls, 1);
});
