import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import test from 'node:test';
import { promisify } from 'node:util';
import { parseNativePermissionTrace, runNativePermissionInteraction } from '../../lib/native-permission-diagnostics.mjs';

const trace = (overrides = {}) => ({
  schema: 'native-permission-diagnostic/v1', stage: 'prompt_found', window_found: true,
  prompt_found: true, invoke_attempted: false, allow_invoked: false, prompt_dismissed: false,
  ...overrides,
});
const readFailure = async (operation) => {
  try { await operation; assert.fail('Expected failure'); }
  catch (error) {
    assert.equal(error.cause, undefined);
    assert.match(error.message, /^Native permission interaction failed: /u);
    return JSON.parse(error.message.slice(error.message.indexOf('{')));
  }
};
function timers() {
  const active = new Map();
  let next = 0;
  return {
    active,
    schedule(callback, delay) { const id = ++next; active.set(id, { callback, delay }); return id; },
    cancel(id) { active.delete(id); },
    fire(delay) {
      const entry = [...active].find(([, value]) => value.delay === delay);
      assert.ok(entry, `Missing ${delay}ms timer`);
      active.delete(entry[0]); entry[1].callback();
    },
  };
}

async function streamedFailure(chunks) {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.pid = 123;
  let reject;
  const command = Object.assign(new Promise((resolve, fail) => { reject = fail; }), { child });
  return readFailure(runNativePermissionInteraction({
    run() {
      queueMicrotask(() => {
        child.emit('spawn');
        for (const chunk of chunks) child.stdout.write(chunk);
        child.stdout.end();
        const error = Object.assign(new Error('private command'), {
          code: '__proto__', stdout: 'private buffered output', stderr: 'private UI tree',
        });
        child.emit('error', error);
        reject(error);
        child.emit('close', 1, null);
      });
      return command;
    }, readPermission: async () => false,
  }));
}

test('trace parser accepts only bounded closed records and returns the last stage', () => {
  const final = trace({ stage: 'allow_returned', invoke_attempted: true, allow_invoked: true });
  assert.deepEqual(parseNativePermissionTrace(`${JSON.stringify(trace())}\r\n${JSON.stringify(final)}\r\n`), final);
  for (const value of [undefined, '', 'raw command', '{}', '[]', 'x'.repeat(4097),
    JSON.stringify(trace({ extra: 'private' })), JSON.stringify(trace({ stage: 'private' })),
    JSON.stringify(trace({ prompt_found: 'yes' })), `${JSON.stringify(trace())}\nraw stderr`,
    Array(9).fill(JSON.stringify(trace())).join('\n')]) {
    assert.equal(parseNativePermissionTrace(value), null);
  }
});

test('successful native interaction preserves its result and reads no permission state', async () => {
  const timer = timers(); let reads = 0;
  const result = await runNativePermissionInteraction({ ...timer,
    run: async (signal) => { assert.equal(signal.aborted, false); return 'done'; },
    readPermission: () => { reads += 1; },
  });
  assert.equal(result, 'done'); assert.equal(reads, 0); assert.equal(timer.active.size, 0);
});

test('failure strips command and stderr and remains failed even when permission is granted', async () => {
  const timer = timers(); let runs = 0; let reads = 0;
  const result = await readFailure(runNativePermissionInteraction({ ...timer, requestKind: 'history',
    run: async () => { runs += 1; throw Object.assign(new Error('secret command'), {
      stdout: JSON.stringify(trace()), stderr: 'private UI tree', code: 1, signal: 'private', killed: false,
    }); },
    readPermission: async () => { reads += 1; return true; },
  }));
  assert.equal(result.request_kind, 'history'); assert.equal(result.stage, 'prompt_found');
  assert.equal(result.exit_code, 1); assert.equal(result.signal, null);
  assert.equal(result.permission_state, 'granted'); assert.equal(result.timeout_fired, false);
  assert.equal(result.trace_source, 'buffered'); assert.equal(result.trace_status, 'complete');
  assert.equal(result.child.present, false); assert.equal(result.child.close_observed, false);
  assert.equal(JSON.stringify(result).includes('private'), false);
  assert.equal(runs, 1); assert.equal(reads, 1); assert.equal(timer.active.size, 0);
});

test('stream diagnostics preserve the last closed stage across malformed and incomplete output', async () => {
  const line = `${JSON.stringify(trace())}\r\n`;
  const complete = await streamedFailure([...Buffer.from(line)].map(byte => Buffer.from([byte])));
  assert.equal(complete.stage, 'prompt_found'); assert.equal(complete.trace_status, 'complete');
  assert.equal(complete.trace_source, 'stream'); assert.equal(complete.trace_bytes, Buffer.byteLength(line));
  assert.deepEqual(complete.child, { present: true, pid: 123, spawn_observed: true,
    error_kind: 'other', close_observed: true, close_exit_code: 1, close_signal: null });
  for (const chunks of [[line, 'private output\n'], ['private output\n', line],
    [line, `${JSON.stringify(trace({ extra: 'private' }))}\n`],
    [line, `${JSON.stringify(trace({ stage: 'private' }))}\n`]]) {
    const result = await streamedFailure(chunks);
    assert.equal(result.stage, 'prompt_found'); assert.equal(result.trace_status, 'invalid');
    assert.equal(result.trace_incomplete, false);
    assert.doesNotMatch(JSON.stringify(result), /private|__proto__/u);
  }
  const incomplete = await streamedFailure([line, '{"schema":']);
  assert.equal(incomplete.stage, 'prompt_found'); assert.equal(incomplete.trace_status, 'incomplete');
  assert.equal(incomplete.trace_incomplete, true);
  const empty = await streamedFailure([]);
  assert.equal(empty.stage, 'unknown'); assert.equal(empty.trace_status, 'empty');
});

test('stream diagnostics bound bytes and records without discarding earlier validated stages', async () => {
  const line = `${JSON.stringify(trace())}\n`;
  const overflow = await streamedFailure([line, 'private'.repeat(100_000)]);
  assert.equal(overflow.stage, 'prompt_found'); assert.equal(overflow.trace_status, 'limit_exceeded');
  assert.equal(overflow.trace_bytes, 4097);
  assert.doesNotMatch(JSON.stringify(overflow), /private/u);
  const records = await streamedFailure([line.repeat(8), `${JSON.stringify(trace({ stage: 'prompt_dismissed' }))}\n`]);
  assert.equal(records.stage, 'prompt_found'); assert.equal(records.trace_status, 'limit_exceeded');
});

test('an abort is not reported until the owned child and pipes close', async () => {
  const timer = timers(), child = new EventEmitter();
  child.stdout = new PassThrough();
  let reject, settled = false, reads = 0;
  const command = Object.assign(new Promise((resolve, fail) => { reject = fail; }), { child });
  const pending = readFailure(runNativePermissionInteraction({ ...timer,
    run(signal) {
      signal.addEventListener('abort', () => {
        const error = Object.assign(new Error('private abort'), { code: 'ABORT_ERR' });
        child.emit('error', error); reject(error);
      }, { once: true });
      return command;
    }, readPermission: async () => { reads += 1; return true; },
  })).then(value => { settled = true; return value; });
  child.emit('spawn');
  child.stdout.write(`${JSON.stringify(trace())}\n`);
  timer.fire(15_000);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(settled, false); assert.equal(reads, 0);
  child.stdout.end(); child.emit('close', null, 'SIGTERM');
  const result = await pending;
  assert.equal(result.child.close_observed, true); assert.equal(result.child.close_exit_code, null);
  assert.equal(result.child.close_signal, 'SIGTERM'); assert.equal(result.child.error_kind, 'aborted');
  assert.equal(result.stage, 'prompt_found'); assert.equal(result.trace_source, 'stream');
  assert.equal(result.timeout_fired, true); assert.equal(result.permission_state, 'granted');
  assert.equal(timer.active.size, 0);
  for (const event of ['spawn', 'error', 'close']) assert.equal(child.listenerCount(event), 0);
  assert.equal(child.stdout.listenerCount('data'), 0);
});

test('a deadline remains a failure when an operation resolves after cancellation', async () => {
  const timer = timers(); let resolve;
  const pending = readFailure(runNativePermissionInteraction({ ...timer,
    run: () => new Promise(done => { resolve = done; }), readPermission: async () => true,
  }));
  timer.fire(15_000); resolve('late success');
  const result = await pending;
  assert.equal(result.timeout_fired, true); assert.equal(result.operation_error_kind, 'aborted');
  assert.equal(result.permission_state, 'granted'); assert.equal(result.child.close_observed, false);
});

test('the explicit 15-second deadline aborts only the owned operation once', async () => {
  const timer = timers(); let runs = 0; let aborts = 0;
  const pending = readFailure(runNativePermissionInteraction({ ...timer, requestKind: 'full',
    run: (signal) => new Promise((resolve, reject) => {
      runs += 1;
      signal.addEventListener('abort', () => { aborts += 1; reject(Object.assign(new Error('raw'), {
        code: 'ABORT_ERR', killed: true, signal: 'SIGTERM', stdout: JSON.stringify(trace()),
      })); }, { once: true });
    }), readPermission: async () => false,
  }));
  timer.fire(15_000);
  const result = await pending;
  assert.equal(result.timeout_fired, true); assert.equal(result.exit_code, null);
  assert.equal(result.killed, true); assert.equal(result.signal, 'SIGTERM');
  assert.equal(result.permission_state, 'denied'); assert.equal(runs, 1); assert.equal(aborts, 1);
  assert.equal(timer.active.size, 0);
});

test('permission diagnosis has one bounded read and cannot hang or leak its error', async () => {
  const timer = timers(); let reads = 0;
  const pending = readFailure(runNativePermissionInteraction({ ...timer, requestKind: 'untrusted name',
    run: async () => { throw Object.assign(new Error('raw command'), { stdout: 'private output' }); },
    readPermission: () => { reads += 1; return new Promise(() => {}); },
  }));
  await new Promise(resolve => setImmediate(resolve));
  timer.fire(1000);
  const result = await pending;
  assert.equal(result.stage, 'unknown'); assert.equal(result.request_kind, 'unspecified');
  assert.equal(result.trace_source, 'buffered'); assert.equal(result.trace_status, 'incomplete');
  assert.equal(result.permission_state, 'unavailable'); assert.equal(reads, 1);
  assert.equal(timer.active.size, 0);
  const deniedRead = await readFailure(runNativePermissionInteraction({
    run: async () => { throw new Error('raw'); },
    readPermission: async () => { throw new Error('private'); },
  }));
  assert.equal(deniedRead.permission_state, 'unavailable');
  assert.equal(deniedRead.trace_status, 'unavailable');
});

test('actual aborted subprocess preserves a complete trace and strips private stderr', { timeout: 10_000 }, async () => {
  const execFileAsync = promisify(execFile);
  let command, abort, closed = false;
  const result = await readFailure(runNativePermissionInteraction({
    schedule(callback, delay) {
      if (delay === 15_000) abort = callback;
      return setTimeout(callback, delay === 15_000 ? 2_000 : delay);
    }, cancel: clearTimeout,
    run(signal) {
      const record = JSON.stringify(trace());
      command = execFileAsync(process.execPath, ['-e',
        `process.stdout.write(${JSON.stringify(record + '\n')}); process.stderr.write('private_stderr'); setInterval(() => {}, 1000);`,
      ], { signal, windowsHide: true });
      command.child.once('close', () => { closed = true; });
      let received = '';
      command.child.stdout.on('data', chunk => {
        received += chunk;
        if (received.includes('\n')) abort();
      });
      return command;
    }, readPermission: async () => false,
  })).finally(() => { if (command?.child.exitCode === null) command.child.kill(); });
  assert.equal(result.stage, 'prompt_found');
  assert.equal(result.prompt_found, true);
  assert.equal(result.timeout_fired, true);
  assert.equal(result.permission_state, 'denied');
  assert.equal(closed, true); assert.equal(result.child.close_observed, true);
  assert.equal(result.child.spawn_observed, true); assert.equal(result.child.error_kind, 'aborted');
  assert.equal(result.trace_source, 'stream'); assert.equal(result.trace_status, 'complete');
  assert.doesNotMatch(JSON.stringify(result), /private_/);
});

test('a failed spawn records an error and physical close without claiming a spawned process', async () => {
  const execFileAsync = promisify(execFile);
  let closed = false;
  const result = await readFailure(runNativePermissionInteraction({
    run(signal) {
      const command = execFileAsync('ofca-native-permission-missing-fixture.exe', [], { signal, windowsHide: true });
      command.child.once('close', () => { closed = true; });
      return command;
    }, readPermission: async () => false,
  }));
  assert.equal(closed, true); assert.equal(result.child.present, true);
  assert.equal(result.child.pid, null); assert.equal(result.child.spawn_observed, false);
  assert.equal(result.child.error_kind, 'not_found'); assert.equal(result.child.close_observed, true);
  assert.equal(result.operation_error_kind, 'not_found'); assert.equal(result.timeout_fired, false);
  assert.equal(result.trace_source, 'stream'); assert.equal(result.trace_status, 'empty');
  assert.equal(result.stage, 'unknown');
  assert.doesNotMatch(JSON.stringify(result), /missing-fixture/u);
});

test('Windows PowerShell inert success, failure and abort physically join without exposing raw output',
  { skip: process.platform !== 'win32', timeout: 25_000 }, async t => {
    const execFileAsync = promisify(execFile);
    const script = String.raw`
$diagnostic = [ordered]@{schema='native-permission-diagnostic/v1';stage='started';window_found=$false;prompt_found=$false;invoke_attempted=$false;allow_invoked=$false;prompt_dismissed=$false}
function Write-NativePermissionStage([string]$Stage) {
  $diagnostic.stage = $Stage
  [Console]::Out.WriteLine(($diagnostic | ConvertTo-Json -Compress))
  [Console]::Out.Flush()
}
Write-NativePermissionStage 'started'
`;
    for (const kind of ['success', 'failure', 'abort']) await t.test(kind, async () => {
      let command, abort, closed = false;
      const operation = runNativePermissionInteraction({
        schedule(callback, delay) {
          if (delay === 15_000) abort = callback;
          return setTimeout(callback, delay);
        }, cancel: clearTimeout,
        run(signal) {
          const ending = kind === 'success' ? 'exit 0' : kind === 'failure'
            ? "[Console]::Error.WriteLine('private_stderr'); throw 'private_exception'" : 'Start-Sleep -Seconds 60';
          command = execFileAsync('powershell.exe', ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script + ending],
            { signal, windowsHide: true, maxBuffer: 16 * 1024 });
          command.child.once('close', () => { closed = true; });
          if (kind === 'abort') {
            let output = '';
            command.child.stdout.on('data', chunk => {
              output += chunk;
              if (output.includes('\n')) abort();
            });
          }
          return command;
        }, readPermission: async () => false,
      });
      if (kind === 'success') {
        const result = await operation;
        assert.equal(parseNativePermissionTrace(result.stdout).stage, 'started');
      } else {
        const result = await readFailure(operation);
        assert.equal(result.stage, 'started'); assert.equal(result.trace_status, 'complete');
        assert.equal(result.trace_source, 'stream'); assert.equal(result.child.spawn_observed, true);
        assert.equal(result.child.close_observed, true);
        assert.equal(result.timeout_fired, kind === 'abort');
        assert.equal(result.permission_state, 'denied');
        if (kind === 'failure') assert.equal(result.child.close_exit_code, 1);
        else {
          assert.equal(result.child.error_kind, 'aborted');
          assert.equal(result.child.close_signal, 'SIGTERM');
        }
        assert.doesNotMatch(JSON.stringify(result), /private_/u);
      }
      assert.equal(closed, true);
    });
  });
