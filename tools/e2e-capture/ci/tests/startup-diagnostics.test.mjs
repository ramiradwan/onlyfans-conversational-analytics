import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import test from 'node:test';
import { createStartupObservations, startupDiagnosticFromMessage, validStartupDiagnostic } from '../../lib/startup-diagnostics.mjs';
import { brainHealthIsReady, fixtureLoopExceptions, fixtureProactorCallbackFailure, lastFixtureListenerResidual, lastFixtureShutdownPhase } from '../../lib/brain.mjs';

function fixture(owner = 'brain') {
  let time = 0;
  const child = new EventEmitter(); child.pid = 123;
  child.stdout = new PassThrough(); child.stderr = new PassThrough();
  const observations = createStartupObservations(owner, { now: () => time });
  observations.bind(child);
  return { child, observations, at(value) { time = value; } };
}

test('shutdown phases retain only the last closed canonical record', () => {
  const phases = ['command_seen', 'control_validated', 'control_rejected', 'server_shutdown_entered',
    'application_shutdown_entered', 'application_shutdown_returned', 'server_returned',
    'owner_close_returned', 'fixture_coroutine_returning', 'event_loop_returned',
    'server_tasks_wait_entered', 'server_waiting_connections', 'server_waiting_tasks',
    'server_listener_wait_entered', 'server_listener_wait_returned', 'server_tasks_wait_returned'];
  const line = phase => JSON.stringify({ schema: 'browser-fixture-shutdown-phase.v1', phase });
  for (const phase of phases) assert.equal(lastFixtureShutdownPhase(line(phase)), phase);
  assert.equal(lastFixtureShutdownPhase(`${line('command_seen')}\r\nprivate output\n${line('control_validated')}\n`), 'control_validated');
  assert.equal(lastFixtureShutdownPhase(`${'private'.repeat(20_000)}\n${line('server_returned')}\n`), 'server_returned');
  assert.equal(lastFixtureShutdownPhase(`${line('owner_close_returned')}\n${line('event_loop_returned').slice(0, -1)}`), 'owner_close_returned');
  assert.equal(lastFixtureShutdownPhase(phases.slice(-6).map(line).join('\n')), 'server_tasks_wait_returned');
});

test('shutdown phase extraction refuses altered fields and unbounded records', () => {
  const value = { schema: 'browser-fixture-shutdown-phase.v1', phase: 'command_seen' };
  const line = JSON.stringify(value);
  for (const output of [null, undefined, {}, [], 'private output', 'x'.repeat(100_000),
    JSON.stringify({ ...value, private: 'content' }), JSON.stringify({ ...value, phase: 'private' }),
    JSON.stringify({ ...value, schema: 'other' }), JSON.stringify({ ...value, phase: null }),
    JSON.stringify([value]), JSON.stringify({ phase: value.phase, schema: value.schema }),
    ' ' + line, line + ' ', JSON.stringify(value, null, 2), line + 'private',
    line.replace('command_seen', 'command\\u005fseen'),
    line.replace('"phase":', '"phase":"command_seen","phase":'),
    line.replace('"phase":"command_seen"', '"phase":NaN'),
    line.replace('command_seen', 'x'.repeat(300)),
    line.replace('command_seen', 'server_waiting_tasks_private'),
    JSON.stringify({ ...value, phase: 'server_waiting_connections', connections: 1 })]) {
    assert.equal(lastFixtureShutdownPhase(output), null);
  }
});

test('listener residual extraction retains only bounded canonical transport counts', () => {
  const value = { schema: 'browser-fixture-listener-residual.v1', transports: 2, closing: 2,
    pending_write: 1, buffered: 1, closed_socket: 0, called_connection_lost: 0 };
  const line = JSON.stringify(value);
  assert.deepEqual(lastFixtureListenerResidual(line), value);
  assert.deepEqual(lastFixtureListenerResidual(`private output\n${line}\r\n`), value);
  assert.deepEqual(lastFixtureListenerResidual(`${'x'.repeat(100_000)}\n${line}\n`), value);
  assert.deepEqual(lastFixtureListenerResidual(`${line}\n${line.slice(0, -1)}`), value);
  const unavailable = Object.fromEntries(Object.keys(value).map(key => [key, key === 'schema' ? value.schema : null]));
  assert.deepEqual(lastFixtureListenerResidual(JSON.stringify(unavailable)), unavailable);
  for (const output of [null, undefined, {}, [], 'private output', 'x'.repeat(100_000),
    JSON.stringify({ ...value, private: 'content' }), JSON.stringify({ ...value, schema: 'other' }),
    JSON.stringify([value]), JSON.stringify({ ...value, closing: 'private' }),
    JSON.stringify({ ...value, closing: true }), JSON.stringify({ ...value, closing: -1 }),
    JSON.stringify({ ...value, closing: 3 }), JSON.stringify({ ...value, transports: 4097 }),
    JSON.stringify({ ...value, transports: null }), JSON.stringify({ ...value, pending_write: 0.5 }),
    JSON.stringify({ ...value, pending_write: Number.MAX_SAFE_INTEGER + 1 }),
    ' ' + line, line + ' ', JSON.stringify(value, null, 2), line + 'private',
    line.replace('"closing":2', '"closing":2,"closing":2'),
    line.replace('"closing":2', '"closing":NaN'), line.replace('closing', 'clo\\u0073ing'),
    line.replace('"closing":2', '"closing":"' + 'x'.repeat(600) + '"')]) {
    assert.equal(lastFixtureListenerResidual(output), null);
  }
});

test('callback failure extraction retains only recognized sites, kinds and bounded error codes', () => {
  const trace = (source = 'self._sock.shutdown(socket.SHUT_RDWR)', exception = 'OSError: [WinError 10057] private detail', method = '_call_connection_lost') => {
    const owner = method === '_call_connection_lost' ? '_ProactorBasePipeTransport' : '_ProactorBaseWritePipeTransport';
    return `Exception in callback ${owner}.${method}() at C:\\Python313\\Lib\\asyncio\\proactor_events.py:154\n`
      + `handle: <Handle ${owner}.${method}() at C:\\Python313\\Lib\\asyncio\\proactor_events.py:154>\n`
      + 'Traceback (most recent call last):\n'
      + '  File "C:\\Python313\\Lib\\asyncio\\events.py", line 89, in _run\n'
      + '    self._context.run(self._callback, *self._args)\n'
      + '    ~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^\n'
      + `  File "C:\\Python313\\Lib\\asyncio\\proactor_events.py", line 165, in ${method}\n`
      + `    ${source}\n    ~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^\n${exception}\n`;
  };
  const standard = trace();
  assert.deepEqual(fixtureProactorCallbackFailure(standard), { site: 'socket_shutdown', kind: 'OSError', errno: null, winerror: 10057 });
  const boundMethod = standard.replaceAll(' at C:\\Python313\\Lib\\asyncio\\proactor_events.py:154', '');
  assert.deepEqual(fixtureProactorCallbackFailure(boundMethod), fixtureProactorCallbackFailure(standard));
  assert.deepEqual(fixtureProactorCallbackFailure(trace('self._sock.close()', 'OSError: [Errno 9] private detail')),
    { site: 'socket_close', kind: 'OSError', errno: 9, winerror: null });
  assert.deepEqual(fixtureProactorCallbackFailure(trace('f.result()', 'asyncio.exceptions.CancelledError', '_loop_writing')),
    { site: 'write_result', kind: 'CancelledError', errno: null, winerror: null });
  assert.deepEqual(fixtureProactorCallbackFailure(trace('self._sock.close()', 'OSError: [Errno 2147483647] private')),
    { site: 'socket_close', kind: 'OSError', errno: 2147483647, winerror: null });
  assert.deepEqual(fixtureProactorCallbackFailure('ERROR:asyncio:' + standard), fixtureProactorCallbackFailure(standard));
  assert.deepEqual(fixtureProactorCallbackFailure(standard.replaceAll('\n', '\r\n')), fixtureProactorCallbackFailure(standard));
  assert.deepEqual(fixtureProactorCallbackFailure(`${'private'.repeat(20_000)}\n${standard}`), fixtureProactorCallbackFailure(standard));
  assert.doesNotMatch(JSON.stringify(fixtureProactorCallbackFailure(standard)), /private|Python313|Handle|SHUT_RDWR/);
  for (const output of [null, undefined, {}, [], 'private output', standard.trimEnd(), standard + standard,
    standard + 'Exception in callback _ProactorBasePipeTransport._call_connection_lost(',
    standard.replace('Traceback (most recent call last):', 'private'),
    standard.replace('    self._sock.shutdown(socket.SHUT_RDWR)', '    private()'),
    standard.replaceAll('_call_connection_lost', 'private'),
    standard.replaceAll('proactor_events.py', 'private.py'), standard.replace('in _run', 'in private'),
    trace('f.result()', 'OSError: [Errno 9] private detail'), trace('self._sock.close()', 'PrivateError: private'),
    trace('self._sock.close()', 'OSError: [WinError private] private'),
    trace('self._sock.close()', 'OSError: [WinError 2147483648] private'),
    trace('self._sock.close()', 'OSError: [Errno -1] private'),
    trace('self._sock.close()', 'OSError: private [Errno 9]'),
    standard.replace('handle: <Handle', 'private: <Handle'),
    standard.replace('Traceback (most recent call last):', 'private\nTraceback (most recent call last):'),
    standard.replace('private detail', 'x'.repeat(3000)),
    standard.replace('OSError:', '  File "C:\\private\\other.py", line 1, in unrelated\n    private()\nOSError:'),
    standard + 'During handling of the above exception, another exception occurred:\n',
    standard.replace('    ~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^', '    private continuation'),
    standard + '\n'.repeat(1100), standard.slice(0, standard.indexOf('OSError:'))]) {
    assert.equal(fixtureProactorCallbackFailure(output), null);
  }
});

test('loop exception records retain only four closed canonical code observations', () => {
  const value = { schema: 'browser-fixture-loop-exception.v1', callback: 'connection_lost',
    kind: 'OSError', line_offset: 11, errno: null, winerror: 10057 };
  const line = JSON.stringify(value);
  const unknown = { schema: value.schema, callback: 'other', kind: 'other', line_offset: null, errno: null, winerror: null };
  assert.deepEqual(fixtureLoopExceptions(`${line}\n`), [value]);
  assert.deepEqual(fixtureLoopExceptions(`${'private'.repeat(20_000)}\n${line}\r\n`), [value]);
  assert.deepEqual(fixtureLoopExceptions(Array(4).fill(line).join('\n')), Array(4).fill(value));
  assert.deepEqual(fixtureLoopExceptions(JSON.stringify(unknown)), [unknown]);
  for (const callback of ['read', 'write']) {
    const record = { ...value, callback, kind: 'CancelledError', errno: null, winerror: null };
    assert.deepEqual(fixtureLoopExceptions(JSON.stringify(record)), [record]);
  }
  for (const output of [null, undefined, {}, [], 'private output', line.slice(0, -1),
    Array(5).fill(line).join('\n'), JSON.stringify({ ...value, raw: 'private' }),
    JSON.stringify({ ...value, callback: 'private' }), JSON.stringify({ ...value, kind: 'PrivateError' }),
    JSON.stringify({ ...value, line_offset: 513 }), JSON.stringify({ ...value, line_offset: -1 }),
    JSON.stringify({ ...value, line_offset: 0.5 }), JSON.stringify({ ...value, errno: true }),
    JSON.stringify({ ...value, winerror: 'private' }), JSON.stringify({ ...value, winerror: 2_147_483_648 }),
    JSON.stringify({ ...value, winerror: -1 }), JSON.stringify({ ...unknown, kind: 'OSError' }),
    JSON.stringify({ ...unknown, errno: 9 }), ' ' + line, line + ' ',
    line.replace('"line_offset":11', '"line_offset":11,"line_offset":11'),
    line.replace('"line_offset":11', '"line_offset":NaN'),
    line.replace('connection_lost', 'connection\\u005flost'),
    line.replace('OSError', 'x'.repeat(600))]) {
    assert.deepEqual(fixtureLoopExceptions(output), []);
  }
});

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
