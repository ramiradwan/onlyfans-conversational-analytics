import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import test from 'node:test';
import { createStartupObservations, startupDiagnosticFromMessage, validStartupDiagnostic } from '../../lib/startup-diagnostics.mjs';
import { brainHealthIsReady } from '../../lib/brain.mjs';

function fixture(owner = 'brain') {
  let time = 0;
  const child = new EventEmitter(); child.pid = 123;
  child.stdout = new PassThrough(); child.stderr = new PassThrough();
  const observations = createStartupObservations(owner, { now: () => time });
  observations.bind(child);
  return { child, observations, at(value) { time = value; } };
}

test('silent startup distinguishes spawn, exit and pipe close without claiming a join', () => {
  const f = fixture(); f.at(2); f.child.emit('spawn');
  f.at(20_000); f.child.emit('exit', null, 'SIGTERM');
  const before = f.observations.snapshot();
  assert.equal(before.spawn_ms, 2); assert.equal(before.exit_ms, 20_000);
  assert.equal(before.close_ms, null); assert.equal(before.first_stdout_ms, null);
  assert.equal(before.phase_status, 'empty'); assert.equal(validStartupDiagnostic(before), true);
  f.at(20_001); f.child.stderr.write('private late error');
  f.at(20_002); f.child.emit('close', null, 'SIGTERM');
  const after = f.observations.snapshot();
  assert.equal(after.first_stderr_ms, 20_001); assert.equal(after.close_ms, 20_002);
  assert.equal(after.close_signal, 'SIGTERM'); assert.doesNotMatch(JSON.stringify(after), /private|late error/);
  assert.equal(f.child.stdout.listenerCount('data'), 0);
});

test('constant entry phases survive chunking; arbitrary output is counted only', () => {
  const f = fixture(); f.at(5); f.child.stdout.write('private content\n');
  const record = JSON.stringify({ schema: 'browser-startup-phase/v1', phase: 'entry_started' }) + '\n';
  f.at(10); for (const byte of Buffer.from(record)) f.child.stdout.write(Buffer.from([byte]));
  f.at(20); f.child.stdout.write(JSON.stringify({ schema: 'browser-startup-phase/v1', phase: 'main_imported' }) + '\n');
  f.child.stdout.write(JSON.stringify({ schema: 'browser-startup-phase/v1', phase: 'private phase' }) + '\n');
  const v = f.observations.snapshot();
  assert.deepEqual(v.phases, [{ phase: 'entry_started', elapsed_ms: 10 }, { phase: 'main_imported', elapsed_ms: 20 }]);
  assert.equal(v.first_stdout_ms, 5); assert.equal(v.phase_status, 'observed');
  assert.doesNotMatch(JSON.stringify(v), /private/); assert.equal(validStartupDiagnostic(v), true);
});

test('unstructured output and counters remain bounded and malformed phases are not copied', () => {
  const f = fixture(); f.child.stdout.write('x'.repeat(100_000));
  f.child.stderr.write('private'.repeat(100_000)); f.child.emit('error', { code: 'private-code', message: 'private' });
  const v = f.observations.snapshot();
  assert.equal(v.stdout_bytes, 65_536); assert.equal(v.stderr_bytes, 65_536);
  assert.equal(v.phase_status, 'limited'); assert.equal(v.error_kind, 'other');
  assert.doesNotMatch(JSON.stringify(v), /private/); assert.equal(validStartupDiagnostic(v), true);
});

test('spawn errors and native phase timings contain only closed classes', () => {
  const f = fixture('native_permission'); f.at(15); f.child.emit('spawn');
  f.at(20); f.child.emit('error', { code: 'ABORT_ERR', message: 'private command' });
  f.at(25); f.observations.phase('started'); f.at(30); f.observations.phase('window_found');
  f.child.emit('close', null, 'SIGTERM');
  const v = f.observations.snapshot();
  assert.equal(v.error_kind, 'aborted'); assert.equal(v.spawn_ms, 15);
  assert.deepEqual(v.phases, [{ phase: 'started', elapsed_ms: 25 }, { phase: 'window_found', elapsed_ms: 30 }]);
  assert.equal(v.health_attempts, 0); assert.equal(validStartupDiagnostic(v), true);
});

test('message extraction refuses arbitrary fields, strings, duplicates, nonfinite values and oversized records', () => {
  const f = fixture(); const v = f.observations.snapshot();
  const encode = value => 'Brain startup failed: ' + JSON.stringify(value);
  assert.deepEqual(startupDiagnosticFromMessage(encode(v)), v);
  assert.deepEqual(startupDiagnosticFromMessage('Error: ' + encode(v)), v);
  for (const value of [{ ...v, raw: 'private' }, { ...v, reason: 'private' }, { ...v, owner: '__proto__' },
    { ...v, elapsed_ms: Infinity }, { ...v, health_outcome: 'private' }, { ...v, close_ms: 1 },
    { ...v, phases: [{ phase: 'private', elapsed_ms: 0 }] }]) {
    assert.equal(startupDiagnosticFromMessage(encode(value)), null);
  }
  assert.equal(startupDiagnosticFromMessage(encode(v).replace('"owner":"brain"', '"owner":"brain","owner":"brain"')), null);
  assert.equal(startupDiagnosticFromMessage('Brain startup failed: ' + 'x'.repeat(20_000)), null);
  assert.equal(startupDiagnosticFromMessage('private ' + encode(v)), null);
});

test('health probe observations preserve ready and failure decisions and the existing timeout', async () => {
  for (const outcome of ['ready', 'non_2xx', 'connection_refused', 'connection_error', 'timeout']) {
    const f = fixture(); const request = new EventEmitter(); let timeout;
    request.setTimeout = (ms, fn) => { assert.equal(ms, 500); timeout = fn; };
    request.destroy = () => request.emit('error', { code: 'ECONNRESET' });
    const response = new EventEmitter(); response.resume = () => {};
    request.end = () => {
      if (outcome === 'timeout') timeout();
      else if (outcome.startsWith('connection')) request.emit('error', { code: outcome === 'connection_refused' ? 'ECONNREFUSED' : 'private' });
    };
    const ready = brainHealthIsReady(f.observations.health, (options, callback) => {
      assert.equal(options.method, 'GET');
      if (['ready', 'non_2xx'].includes(outcome)) {
        response.statusCode = outcome === 'ready' ? 200 : 503;
        queueMicrotask(() => callback(response));
      }
      return request;
    });
    assert.equal(await ready, outcome === 'ready');
    const v = f.observations.snapshot();
    assert.equal(v.health_attempts, 1); assert.equal(v.health_outcome, outcome);
  }
});
