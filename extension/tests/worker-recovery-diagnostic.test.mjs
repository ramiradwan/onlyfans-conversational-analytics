import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import * as diagnostics from '../../tools/e2e-capture/lib/stable-connection-diagnostic.mjs';
import { AgentRuntime } from '../transport/agent-runtime-core.mjs';

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
