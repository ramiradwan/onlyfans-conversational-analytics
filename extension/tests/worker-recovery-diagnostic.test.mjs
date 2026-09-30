import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import * as diagnostics from '../../tools/e2e-capture/lib/stable-connection-diagnostic.mjs';
import { AgentRuntime } from '../transport/agent-runtime-core.mjs';

for (const [status, body, code] of [
  [409, { detail: 'cursor_stale' }, 'cursor_stale'],
  [409, { detail: 'cursor_invalid' }, 'cursor_invalid'],
  [503, { detail: 'projection_unavailable' }, 'projection_unavailable'],
  [403, { detail: { code: 'access_refused' } }, 'access_refused'],
  [200, {}, 'unavailable'],
  [500, { detail: 'arbitrary response text', items: [{ text: 'arbitrary message text' }] }, 'unavailable'],
  [502, null, 'unavailable'],
]) {
  test(`catchup message diagnostics retain status ${status} and code ${code}`, async () => {
    const { readCatchupMessageIds } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
    let calls = 0;
    const read = runInNewContext(`(${readCatchupMessageIds.toString()})`, { AbortSignal, fetch: async url => {
      calls++;
      assert.equal(url, '/api/v1/conversations/103/messages?limit=100');
      return { status, ok: status === 200, async json() {
        if (body === null) throw new SyntaxError('arbitrary response text');
        return body;
      } };
    } });
    await assert.rejects(read('103'), error => {
      assert.equal(error.message, `Catch-up messages unavailable: chat=103 status=${status} code=${code}`);
      return true;
    });
    assert.equal(calls, code === 'cursor_stale' ? 2 : 1);
  });
}

test('catchup message diagnostics preserve readable message identifiers', async () => {
  const { readCatchupMessageIds } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
  const read = runInNewContext(`(${readCatchupMessageIds.toString()})`, { AbortSignal, fetch: async () => ({
    status: 200, ok: true, json: async () => ({ items: [{ message_id: 'missing103' }] }),
  }) });
  assert.equal(JSON.stringify(await read('103')), '["missing103"]');
});

test('catchup message read retries a stale first page once without a cursor', async () => {
  const { readCatchupMessageIds } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
  const requests = [];
  const read = runInNewContext(`(${readCatchupMessageIds.toString()})`, { AbortSignal, fetch: async (url, options) => {
    requests.push({ url, signal: options?.signal });
    return requests.length === 1
      ? { status: 409, ok: false, json: async () => ({ detail: 'cursor_stale' }) }
      : { status: 200, ok: true, json: async () => ({ items: [{ message_id: 'missing102' }] }) };
  } });
  assert.equal(JSON.stringify(await read('102')), '["missing102"]');
  assert.deepEqual(requests.map(request => request.url), Array(2).fill('/api/v1/conversations/102/messages?limit=100'));
  assert.ok(requests[0].signal instanceof AbortSignal);
  assert.equal(requests[0].signal, requests[1].signal);
});

test('catchup message read reports the final response when its one retry fails', async () => {
  const { readCatchupMessageIds } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
  let calls = 0;
  const read = runInNewContext(`(${readCatchupMessageIds.toString()})`, { AbortSignal, fetch: async () => {
    calls++;
    return calls === 1
      ? { status: 409, ok: false, json: async () => ({ detail: 'cursor_stale' }) }
      : { status: 503, ok: false, json: async () => ({ detail: 'projection_unavailable' }) };
  } });
  await assert.rejects(read('102'), { message: 'Catch-up messages unavailable: chat=102 status=503 code=projection_unavailable' });
  assert.equal(calls, 2);
});

test('catchup message read shares a ten-second deadline across the stale retry', async () => {
  const { readCatchupMessageIds } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
  const controller = new AbortController();
  let calls = 0, deadlines = 0;
  const read = runInNewContext(`(${readCatchupMessageIds.toString()})`, {
    AbortSignal: { timeout(milliseconds) {
      assert.equal(milliseconds, 10_000);
      deadlines++;
      return controller.signal;
    } },
    fetch: async (_url, options) => {
      calls++;
      if (calls === 1) return { status: 409, ok: false, json: async () => ({ detail: 'cursor_stale' }) };
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('arbitrary network detail')), { once: true });
        controller.abort();
      });
    },
  });
  await assert.rejects(read('102'), { message: 'Catch-up messages unavailable: chat=102 status=409 code=cursor_stale' });
  assert.equal(calls, 2);
  assert.equal(deadlines, 1);
});

test('session task provenance survives collection without arbitrary data', () => {
  const collector = diagnostics.createSessionFailureCollector();
  collector.append('e2e-session-failure: ' + JSON.stringify({ method: 'session.serve',
    causes: [{ errorName: 'QueueFull', phase: 'broadcast_catchup', message: 'private_message',
      frames: [{ module: 'channel', line: 81 }, { module: 'agent_socket', line: 44 }] }] }) + '\n');
  const [entry] = collector.snapshot();
  assert.equal(entry.method, 'session.serve');
  assert.equal(entry.causes[0].errorName, 'QueueFull');
  assert.equal(entry.causes[0].phase, 'broadcast_catchup');
  assert.equal(entry.causes[0].frames[0].module, 'channel');
  assert.equal(entry.causes[0].frames[1].module, 'agent_socket');
  assert.doesNotMatch(JSON.stringify(entry), /private_/);
});

test('successful recovery checkpoints retain rotation evidence with bounded redaction', () => {
  const output = [];
  diagnostics.logWorkerRecoveryCheckpoint('alarm', { token: 'private_token',
    credentialRotation: { attempts: 2, completed: 1, failure: { phase: 'rpc', cause: 'session_request_refused' } },
    connectionEvents: Array.from({ length: 40 }, () => ({ event: 'facade-close', reason: 'credential_store_failed' })),
  }, [{ method: 'agent.storage.rotate', causes: [], token: 'private_token' }], line => output.push(line));
  assert.equal(output.length, 1);
  const report = JSON.parse(output[0].split('worker-recovery-checkpoint: ')[1]);
  assert.equal(report.stage, 'alarm');
  assert.equal(report.recovery.credentialRotation.completed, 1);
  assert.equal(report.recovery.credentialRotation.failure.cause, 'session_request_refused');
  assert.equal(report.recovery.connectionEvents.length, 24);
  assert.equal(report.brainSessionFailures[0].method, 'agent.storage.rotate');
  assert.doesNotMatch(output[0], /private_/);
  diagnostics.logWorkerRecoveryCheckpoint('private_stage', {}, [], line => output.push(line));
  assert.equal(output.length, 1);
  assert.doesNotThrow(() => diagnostics.logWorkerRecoveryCheckpoint('alarm', {}, [], () => { throw Error(); }));
});

test('successful initial history checkpoint exposes setup order without changing its result', async () => {
  const { withCatchupDiagnostics } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
  const output = [], result = {};
  assert.equal(await withCatchupDiagnostics(async () => result, {
    checkpoint: 'initial_history', summary: async () => ({ account: 'private_account' }),
    platform: { requestCounts: {} }, readWorker: async () => ({ shim: { setupTrace: [
      { event: 'notification', sessionPresent: false, configurationEnabled: true, configurationApplied: false, token: 'private_token' },
      { event: 'rpc', operation: 'capture.state.report', sessionPresent: true, configurationEnabled: true, configurationApplied: true },
    ] } }), log: line => output.push(line),
  }), result);
  assert.equal(output.length, 1);
  assert.match(output[0], /^Catch-up checkpoint initial_history /);
  assert.match(output[0], /"sessionPresent":false/);
  assert.match(output[0], /"operation":"capture.state.report"/);
  assert.doesNotMatch(output[0], /private_/);
});

test('setup trace is bounded and observes notification and RPC admission independently', async () => {
  const { installCatchupShim, catchupDiagnosticFields } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
  let session = null, enabled = false;
  const result = Promise.resolve('unchanged');
  const catchup = { configuration: () => ({ config_revision: 'private_revision', history_acquisition: { enabled } }),
    session: () => session, requestCaptureStateReport() { assert.equal(this, catchup); return result; },
    rpc() { assert.equal(this, catchup); return result; } };
  const transport = { onSession() { assert.equal(this, transport); return 'admitted'; } };
  const components = { history: { initial: {}, catchup }, transport };
  const runtime = { async initialize() { return components; } }, root = {};
  installCatchupShim(runtime, {}, root, async () => ({}));
  assert.equal(await runtime.initialize(), components);
  assert.equal(catchup.requestCaptureStateReport(), result);
  enabled = true;
  session = { applied_config_revision: null };
  assert.equal(transport.onSession(), 'admitted');
  session.applied_config_revision = 'private_revision';
  assert.equal(catchup.rpc('capture.state.report', { token: 'private_token' }), result);
  const trace = root.__OFCA_CATCHUP_SHIM__.setupTrace;
  assert.deepEqual(trace.map(entry => entry.event), ['initialized', 'notification', 'admission', 'rpc']);
  assert.equal(trace[1].sessionPresent, false);
  assert.equal(trace[2].configurationApplied, false);
  assert.equal(trace[3].configurationApplied, true);
  for (let index = 0; index < 40; index++) catchup.rpc('private_method');
  assert.equal(trace.length, 32);
  const report = catchupDiagnosticFields(null, null, { shim: root.__OFCA_CATCHUP_SHIM__ });
  assert.equal(report.shim.setupTrace.length, 32);
  assert.doesNotMatch(JSON.stringify(report), /private_/);
});

test('successful checkpoint diagnostics bound unreadable state and preserve the assertion result', async () => {
  const { withCatchupDiagnostics } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
  let logs = 0;
  const result = await withCatchupDiagnostics(async () => 7, {
    checkpoint: 'initial_history', summary: () => new Promise(() => {}), readWorker: () => new Promise(() => {}),
    platform: { requestCounts: {} }, timeoutMs: 1, log() { logs++; throw new Error('private_sink'); },
  });
  assert.equal(result, 7);
  assert.equal(logs, 1);
});

test('worker recovery diagnostics are bounded and exclude arbitrary snapshot data', () => {
  const report = diagnostics.buildWorkerRecoveryDiagnostic({
    capturedAt: 100, runtimeReady: true, transportStopped: false, reconnectAllowed: false,
    socketOpen: false, sessionBound: false, reconnectTimerPresent: false,
    recoveryAttempts: 3, recoveryNextAttemptInMs: 0,
    reconcileAlarm: { scheduledTime: 60_100, periodInMinutes: 1, name: 'private_alarm' },
    outbox: { acknowledgedSourceSeq: 8, pendingEntries: 0 },
    workerInstanceId: 'private_worker', url: 'private_url', token: 'private_token',
    connectionEvents: Array.from({ length: 40 }, () => ({ event: 'facade-close', code: 4008,
      reason: 'credential_store_failed', message: 'private_message' })),
  });
  assert.equal(report.reconnectAllowed, false);
  assert.equal(report.transportStopped, false);
  assert.equal(report.alarmDueInMs, 60_000);
  assert.equal(report.acknowledgedSourceSeq, 8);
  assert.equal(report.connectionEvents.length, 24);
  assert.equal(report.connectionEvents[0].reason, 'credential_store_failed');
  assert.doesNotMatch(JSON.stringify(report), /private_/);
  const unknown = diagnostics.buildWorkerRecoveryDiagnostic({ connectionEvents: [{ event: 'private_event', reason: 'private_reason' }] });
  assert.equal(unknown.connectionEvents[0].event, 'other');
  assert.equal(unknown.connectionEvents[0].reason, 'other');
});

test('the failing state wait prints only the allowlisted recovery report', async () => {
  const source = await readFile(new URL('../../tools/e2e-capture/tests/capture.spec.mjs', import.meta.url), 'utf8');
  const code = source.slice(source.indexOf('async function waitForExtensionState('), source.indexOf('async function waitForBrain('));
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const wait = new AsyncFunction('expect', 'extensionState', 'buildWorkerRecoveryDiagnostic', `${code}\nreturn waitForExtensionState;`);
  const probe = { poll: (fn) => ({ async toBe() { await fn(); throw new Error('private_failure'); } }) };
  const handler = await wait(probe, async () => ({ workerInstanceId: 'private_worker', socketOpen: false,
    connectionEvents: [{ event: 'facade-close', reason: 'credential_store_failed' }] }), diagnostics.buildWorkerRecoveryDiagnostic);
  await assert.rejects(handler({ evaluate: async () => ({ token: 'private_identity' }) }, () => false, 'recovery_failed'), (error) => {
    assert.doesNotMatch(`${error.stack}${JSON.stringify(error.cause)}`, /private_/);
    assert.match(error.message, /credential_store_failed/);
    return true;
  });
});

test('both worker snapshots expose stopped and retry authorization state', async () => {
  for (const file of ['background.js', 'background-read-only.js']) {
    const source = await readFile(new URL(`../${file}`, import.meta.url), 'utf8');
    const body = source.slice(source.indexOf('export async function agentDiagnosticSnapshot('), source.indexOf("Object.defineProperty(globalThis, '__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__'"));
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    const snapshot = new AsyncFunction('agentRuntime', 'chrome', 'consentController', 'agentWorkerInstanceId', 'companionClient', 'captureDiagnostics', 'WebSocket',
      `${body.replace('export ', '')}\nreturn agentDiagnosticSnapshot();`);
    const state = await snapshot({ transport: { stopped: true, reconnectAllowed: false } },
      { alarms: { get: async () => undefined }, storage: { local: { get: async () => ({}) } } },
      { status: async () => ({ consent: { mode: 'full' } }) }, 'private_worker', { diagnosticEvents: [] }, { snapshot: () => ({}) }, { OPEN: 1 });
    assert.equal(state.transportStopped, true, file);
    assert.equal(state.reconnectAllowed, false, file);
  }
});

test('startup backoff needs another wake after its delay and before the minute alarm', async () => {
  let time = 0, attempts = 0, wake;
  const runtime = new AgentRuntime({ registerWakeListeners(fn) { wake = fn; }, async initialize() {
    attempts++;
    if (time < 1_000) throw Object.assign(new Error('backoff'), { code: 'companion_recovery_backoff', retryAfterMs: 1_000 });
    return { transport: { start() {} } };
  } });
  assert.deepEqual(await runtime.start(), { retryAfterMs: 1_000 });
  time = 20_000;
  await new Promise(setImmediate);
  assert.equal(attempts, 1);
  assert.equal(runtime.transport, null);
  wake();
  await new Promise(setImmediate);
  assert.equal(attempts, 2);
  assert.ok(runtime.transport);
});


test('recovery output includes sanitized rotation and configuration state', () => {
  const state = {
    credentialRotation: { attempts: 2, completed: 1, failure: { phase: 'rpc', cause: 'session_request_refused',
      errorName: 'CompanionChannelError', signalAborted: false, channelClosed: true, bindingCurrent: false,
      message: 'private_message', token: 'private_token' } },
    configuration: { documentPresent: true, bundled: true, applied: false, required: true,
      revisionsMatch: false, authorized: false, refreshPending: false, retryScheduled: false,
      retryAttempt: 2, failureCode: 'session_request_refused', revision: 'private_revision' },
  };
  const result = diagnostics.buildWorkerRecoveryDiagnostic(state);
  assert.equal(result.credentialRotation.failure.phase, 'rpc');
  assert.equal(result.credentialRotation.failure.cause, 'session_request_refused');
  assert.equal(result.configuration.bundled, true);
  assert.equal(result.configuration.revisionsMatch, false);
  assert.equal(result.configuration.failureCode, 'session_request_refused');
  assert.doesNotMatch(JSON.stringify(result), /private_/);
  state.credentialRotation.failure.phase = 'private_phase';
  state.credentialRotation.failure.cause = 'private_cause';
  state.credentialRotation.failure.errorName = 'private_name';
  state.configuration.failureCode = 'private_code';
  assert.doesNotMatch(JSON.stringify(diagnostics.buildWorkerRecoveryDiagnostic(state)), /private_/);
});

test('both worker snapshots expose rotation failure and configuration progress without revision values', async () => {
  for (const file of ['background.js', 'background-read-only.js']) {
    const source = await readFile(new URL('../' + file, import.meta.url), 'utf8');
    const body = source.slice(source.indexOf('export async function agentDiagnosticSnapshot('), source.indexOf("Object.defineProperty(globalThis, '__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__'"));
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    const snapshot = new AsyncFunction('agentRuntime', 'chrome', 'consentController', 'agentWorkerInstanceId', 'companionClient', 'captureDiagnostics', 'WebSocket',
      body.replace('export ', '') + '\nreturn agentDiagnosticSnapshot();');
    const state = await snapshot({ transport: { stopped: false, reconnectAllowed: false }, configuration: {
      activeDocument: { config_revision: 'bundled-safe-2' }, identity: { appliedConfigRevision: null },
      required: { revision: 'private_revision' }, configAuthTicket: null, refreshPromise: null,
      retryTimer: null, retryAttempt: 0, lastFailure: { code: 'fetch_failed', message: 'private_message' },
    } }, { alarms: { get: async () => undefined }, storage: { local: { get: async () => ({}) } } },
    { status: async () => ({ consent: { mode: 'full' } }) }, 'private_worker',
    { diagnosticEvents: [], credentialRotation: { attempts: 1, completed: 0 } }, { snapshot: () => ({}) }, { OPEN: 1 });
    assert.equal(state.credentialRotation.attempts, 1, file);
    assert.equal(state.configuration.bundled, true, file);
    assert.equal(state.configuration.required, true, file);
    assert.equal(state.configuration.applied, false, file);
    assert.equal(state.configuration.failureCode, 'fetch_failed', file);
    assert.doesNotMatch(JSON.stringify(state.configuration), /private_/);
  }
});

test('catchup failure prints rotation close circuit and configuration evidence', async () => {
  const { withCatchupDiagnostics } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
  const output = [];
  const original = new Error('poll_failed');
  await assert.rejects(withCatchupDiagnostics(async () => { throw original; }, {
    summary: async () => null, platform: { requestCounts: {} }, log: value => output.push(value),
    readWorker: async () => ({ recovery: { reconnectAllowed: false, recoveryAttempts: 6, recoveryNextAttemptInMs: 300000,
      connectionEvents: [{ event: 'facade-close', reason: 'credential_store_failed', code: 4011 }],
      credentialRotation: { attempts: 1, completed: 0, failure: { phase: 'rpc', cause: 'session_request_refused' } },
      configuration: { bundled: true, applied: false, required: true } } }),
  }), error => error === original);
  assert.match(output[0], /"recoveryAttempts":6/);
  assert.match(output[0], /"cause":"session_request_refused"/);
  assert.match(output[0], /"reason":"credential_store_failed"/);
  assert.match(output[0], /"bundled":true/);
});

test('server failure output filters arbitrary fields and bounds causes and frames', () => {
  const entry = { method: 'agent.storage.rotate', causes: Array.from({ length: 8 }, () => ({
    errorName: 'ValueError', phase: 'validate_config', message: 'private_message',
    frames: Array.from({ length: 20 }, () => ({ module: 'authority', line: 123, path: 'private_path' })),
  })) };
  const input = Array.from({ length: 30 }, () => 'e2e-session-failure: ' + JSON.stringify(entry)).join('\n');
  const results = diagnostics.readSessionFailures('private_output\ne2e-session-failure: invalid\n' + input);
  assert.equal(results.length, 24);
  assert.equal(results[0].causes.length, 4);
  assert.equal(results[0].causes[0].frames.length, 8);
  assert.equal(results[0].causes[0].phase, 'validate_config');
  assert.doesNotMatch(JSON.stringify(results), /private_/);
  entry.method = 'private_method';
  entry.causes[0] = { errorName: 'private_error', phase: 'private_phase', frames: [{ module: 'private_module', line: -1 }] };
  assert.doesNotMatch(JSON.stringify(diagnostics.readSessionFailures('e2e-session-failure: ' + JSON.stringify(entry))), /private_/);
});

test('catchup worker reader includes the production recovery snapshot', async () => {
  const { readCatchupWorker } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
  const key = '__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__';
  const previous = globalThis[key];
  try {
    globalThis[key] = async () => ({ reconnectAllowed: false, recoveryAttempts: 6 });
    const worker = { url: () => 'chrome-extension://test/background.js', evaluate: fn => fn() };
    const result = await readCatchupWorker({ serviceWorkers: () => [worker] }, async () => worker);
    assert.equal(result.recovery.recoveryAttempts, 6);
    assert.equal(result.recovery.reconnectAllowed, false);
  } finally {
    if (previous === undefined) delete globalThis[key];
    else globalThis[key] = previous;
  }
});

test('recovery reports channel contention and allowlists its nested fields', () => {
  const state = { credentialRotation: { failure: { phase: 'rpc', channel: {
    cause: 'rpc_capacity', pendingRpcs: 8, queuedSends: 8, token: 'private_token',
  } } }, connectionEvents: [{ event: 'channel-close', channel: {
    cause: 'send_capacity', pendingRpcs: 1, queuedSends: 8, body: 'private_message',
  } }] };
  const report = diagnostics.buildWorkerRecoveryDiagnostic(state);
  assert.equal(report.credentialRotation.failure.channel.cause, 'rpc_capacity');
  assert.equal(report.connectionEvents[0].channel.cause, 'send_capacity');
  assert.equal(report.connectionEvents[0].channel.pendingRpcs, 1);
  assert.doesNotMatch(JSON.stringify(report), /private_/);
  state.connectionEvents[0].channel.cause = 'private_cause';
  assert.doesNotMatch(JSON.stringify(diagnostics.buildWorkerRecoveryDiagnostic(state)), /private_/);
});

test('server failure retention survives rolling output and split chunks', async () => {
  const { BrainProcess } = await import('../../tools/e2e-capture/lib/brain.mjs');
  const brain = new BrainProcess({});
  const line = 'e2e-session-failure: ' + JSON.stringify({ method: 'agent.storage.rotate',
    causes: [{ errorName: 'ValueError', phase: 'validate_config', message: 'private_message' }] }) + '\n';
  brain.rememberOutput(line.slice(0, 17));
  brain.rememberOutput('private_stderr\n', false);
  brain.rememberOutput(line.slice(17));
  for (let index = 0; index < 100; index++) brain.rememberOutput('private_noise\n');
  assert.doesNotMatch(brain.recentOutput(), /e2e-session-failure/);
  assert.equal(brain.sessionFailures()[0].causes[0].phase, 'validate_config');
  assert.doesNotMatch(JSON.stringify(brain.sessionFailures()), /private_/);
  const result = brain.sessionFailures();
  result[0].method = 'private_method';
  assert.equal(brain.sessionFailures()[0].method, 'agent.storage.rotate');
});

test('server failure retention bounds oversized lines and retained reports', () => {
  const collector = diagnostics.createSessionFailureCollector();
  const line = 'e2e-session-failure: ' + JSON.stringify({ method: 'agent.storage.rotate', causes: [] }) + '\n';
  collector.append('e2e-session-failure: ' + 'x'.repeat(20_000));
  collector.append(line);
  assert.equal(collector.snapshot().length, 0);
  collector.append(line.repeat(30));
  assert.equal(collector.snapshot().length, 24);
});

test('catchup failure uses retained server evidence after output eviction', async () => {
  const { withCatchupDiagnostics } = await import('../../tools/e2e-capture/lib/catchup-diagnostics.mjs');
  const output = [];
  await assert.rejects(withCatchupDiagnostics(async () => { throw new Error('poll_failed'); }, {
    summary: async () => null, platform: { requestCounts: {} }, readWorker: async () => null,
    brain: { recentOutput: () => '', sessionFailures: () => [{ method: 'agent.storage.rotate', causes: [] }] },
    log: value => output.push(value),
  }));
  assert.match(output[0], /"brainSessionFailures":\[{"method":"agent.storage.rotate"/);
});
