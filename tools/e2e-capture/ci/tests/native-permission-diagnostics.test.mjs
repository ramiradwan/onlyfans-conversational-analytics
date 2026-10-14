import assert from 'node:assert/strict';
import test from 'node:test';
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
  assert.equal(JSON.stringify(result).includes('private'), false);
  assert.equal(runs, 1); assert.equal(reads, 1); assert.equal(timer.active.size, 0);
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
  await Promise.resolve(); await Promise.resolve();
  timer.fire(1000);
  const result = await pending;
  assert.equal(result.stage, 'unknown'); assert.equal(result.request_kind, 'unspecified');
  assert.equal(result.permission_state, 'unavailable'); assert.equal(reads, 1);
  assert.equal(timer.active.size, 0);
  const deniedRead = await readFailure(runNativePermissionInteraction({
    run: async () => { throw new Error('raw'); },
    readPermission: async () => { throw new Error('private'); },
  }));
  assert.equal(deniedRead.permission_state, 'unavailable');
});
