import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { expect } from '@playwright/test';
import playwrightUtil from '../node_modules/playwright/lib/util.js';
import playwrightExpect from '../node_modules/playwright/lib/matchers/expect.js';
import { base as reporter } from 'playwright/lib/runner';
import { buildStableConnectionDiagnostic, redactStableConnectionAssertionError, withoutReportedExpectStep } from '../../tools/e2e-capture/lib/stable-connection-diagnostic.mjs';

const { serializeError } = playwrightUtil;

async function renderStableFailure(actual = null) {
  const source = await readFile(new URL('../../tools/e2e-capture/tests/capture.spec.mjs', import.meta.url), 'utf8');
  const step = source.split("test.step('a stable connection resets persisted recovery history through normal UI polling'")[1];
  const handler = step.split('} catch (error) {')[1].split('} finally { watcher.stop(); }')[0];
  const initialConnection = 'synthetic_expected_token';
  let error;
  try { expect(actual).toBe(initialConnection); } catch (failure) { error = failure; }
  const worker = { url: () => '/background.js' };
  const attachments = [];
  const output = [];
  const dependencies = {
    error, initialConnection, worker,
    summary: { connectionToken: actual, lastHeartbeatAt: '2026-01-01T00:00:00Z', agentStatus: 'private_status' },
    context: { serviceWorkers: () => [worker] },
    statusPolls: 2, lastStatusPollAt: 90, popupClosedAt: 95,
    watcher: { creations: [] },
    extensionState: async () => ({
      connectionToken: initialConnection, workerInstanceId: 'private_worker',
      runtimeReady: true, socketOpen: false, sessionBound: false,
      connectionEvents: Array.from({ length: 24 }, (_, at) => ({
        at, event: 'channel-close', code: 4001, reason: 'private_reason', wasStable: true,
      })),
    }),
    test: { info: () => ({ attach: async (name, options) => {
      attachments.push({ name, ...options, body: Buffer.from(options.body) });
    } }) },
    console: { error: (...values) => output.push(values.join(' ')) },
    buildStableConnectionDiagnostic, redactStableConnectionAssertionError,
  };
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  await assert.rejects(new AsyncFunction(...Object.keys(dependencies), handler)(...Object.values(dependencies)),
    (failure) => failure === error);
  const result = { status: 'failed', retry: 0, errors: [serializeError(error)], attachments, steps: [] };
  const testCase = {
    expectedStatus: 'passed', results: [result], tags: [],
    location: { file: new URL(import.meta.url).pathname, line: 1, column: 1 },
    titlePath: () => ['', '', '', 'stable connection'],
  };
  const formatted = reporter.formatFailure(reporter.nonTerminalScreen, { rootDir: '.', tags: [] }, testCase, 1);
  return { printed: [...output, formatted].join('\n'), output, attachments };
}

test('stable failure prints the complete safe report beyond the reporter attachment limit', async () => {
  const { printed, attachments } = await renderStableFailure();
  const report = attachments[0].body.toString();
  assert.ok(report.length > 300);
  assert.ok(printed.includes(report), 'The complete safe report must reach the job log');
  assert.equal(JSON.parse(report).extension.connectionEvents.length, 24);
});

test('stable failure printed text excludes tokens and unallowlisted fields', async () => {
  for (const actual of [null, 'synthetic_actual_token']) {
    const { printed, output } = await renderStableFailure(actual);
    assert.ok(output.length > 0, 'Exercise the diagnostic log output');
    assert.doesNotMatch(printed, /synthetic_(?:actual|expected)_token|private_/u);
    assert.match(printed, /\[redacted\]/u);
    assert.match(printed, /"connectionPresent":(?:true|false)/u);
    assert.match(printed, /"reason":"other"/u);
  }
});

test('stable token assertion creates no early runner step with raw values', () => {
  const actual = 'synthetic_step_actual_token';
  const expected = 'synthetic_step_expected_token';
  const originalConfig = playwrightExpect.expectConfig();
  const reportedSteps = [];
  const testInfo = {
    _addStep(data) {
      reportedSteps.push(JSON.stringify(data));
      return { complete(result) {
        if (result.error) reportedSteps.push(JSON.stringify(serializeError(result.error)));
      } };
    },
  };
  playwrightExpect.setExpectConfig({ ...originalConfig, testInfo });
  try {
    try { expect(actual).toBe(expected); } catch {}
    assert.ok(reportedSteps.join('').includes(expected));
    reportedSteps.length = 0;
    let failure;
    try { withoutReportedExpectStep(() => expect(actual).toBe(expected), playwrightExpect); }
    catch (error) { failure = error; }
    assert.ok(failure);
    assert.deepEqual(reportedSteps, []);
    const firstFrame = serializeError(failure).stack.split('\n').find((line) => line.trim().startsWith('at '));
    assert.match(firstFrame, /stable-connection-diagnostic\.test\.mjs:\d+:/u);
    assert.equal(playwrightExpect.expectConfig().testInfo, testInfo);
  } finally {
    playwrightExpect.setExpectConfig(originalConfig);
  }
});

test('stable connection assertion redacts every reported token field and retains its frame', () => {
  const actual = 'synthetic_actual_connection_token';
  const expected = 'synthetic_expected_connection_token';
  const originalExpectLine = () => expect(actual).toBe(expected);
  let failure;
  try { originalExpectLine(); }
  catch (error) { failure = error; }
  assert.ok(failure);
  const originalFrame = failure.stack.split('\n').find((line) => line.includes('originalExpectLine ('));
  assert.ok(originalFrame);
  failure.cause = new Error(`cause ${actual} ${expected}`);
  failure.matcherResult.ariaSnapshot = `${actual} ${expected}`;
  const serialize = (error) => {
    const result = serializeError(error);
    if (error.matcherResult?.ariaSnapshot !== undefined) result.errorContext = error.matcherResult.ariaSnapshot;
    return result;
  };
  const unredacted = JSON.stringify({ serialized: serialize(failure), matcherResult: failure.matcherResult });
  assert.ok(unredacted.includes(actual));
  assert.ok(unredacted.includes(expected));
  const redacted = redactStableConnectionAssertionError(failure, actual, expected);
  assert.equal(redacted, failure);
  const reported = JSON.stringify({ serialized: serialize(redacted), matcherResult: redacted.matcherResult });
  assert.equal(reported.includes(actual), false);
  assert.equal(reported.includes(expected), false);
  assert.equal(redacted.matcherResult.actual, undefined);
  assert.equal(redacted.matcherResult.expected, undefined);
  assert.ok(redacted.stack.includes(originalFrame));
  const firstReportedFrame = serialize(redacted).stack.split('\n').find((line) => line.trim().startsWith('at '));
  assert.equal(firstReportedFrame, originalFrame);
});

test('stable connection report contains only derived facts', () => {
  const report = buildStableConnectionDiagnostic({
    at: 100, statusPolls: 2, lastStatusPollAt: 90, popupClosedAt: 95,
    workerStartsDuringStep: 1, originalWorkerAlive: false,
    brain: { status: 'private_status', lastHeartbeatAt: 'private_time', connectionPresent: false },
    extension: {
      workerInstanceId: 'private_worker_id', appliedConfigRevision: 'private_revision',
      runtimeReady: true, socketOpen: false, sessionBound: false,
      lastHeartbeatSentAt: 80, reconnectTimerPresent: true, recoveryAttempts: 2,
      connectionEvents: [{ at: 85, event: 'channel-close', code: 1008, reason: 'private_note', wasStable: true },
        { at: 75, event: 'connect-start' }, { at: 90, event: 'facade-close' }],
      preview: 'private_preview',
    },
    stateError: new Error('private_error_text'),
  });
  assert.equal(report.workerChangedDuringStep, true);
  assert.equal(report.stateError, 'Error');
  assert.equal(report.extension.connectionEvents[0].reason, 'other');
  assert.deepEqual(report.extension.connectionEvents.map((entry) => entry.event), ['channel-close', 'connect-start', 'facade-close']);
  assert.equal(JSON.stringify(report).includes('private_'), false);
  assert.equal(Object.hasOwn(report.extension, 'workerInstanceId'), false);
});

test('stable step keeps the token equality assertion and attaches diagnostics', async () => {
  const source = await readFile(new URL('../../tools/e2e-capture/tests/capture.spec.mjs', import.meta.url), 'utf8');
  const step = source.split("test.step('a stable connection resets persisted recovery history through normal UI polling'")[1].split('let pendingEncryptedOutbox')[0];
  assert.match(step, /expect\(summary\.connectionToken\)\.toBe\(initialConnection\)/u);
  assert.match(step, /withoutReportedExpectStep\(\(\) => expect\(summary\.connectionToken\)\.toBe\(initialConnection\), playwrightExpect\)/u);
  assert.match(step, /test\.info\(\)\.attach\(/u);
  assert.match(step, /redactStableConnectionAssertionError\(error, summary\?\.connectionToken, initialConnection\)/u);
  assert.doesNotMatch(step, /throw new Error\(`Stable connection diagnostic:/u);
});
