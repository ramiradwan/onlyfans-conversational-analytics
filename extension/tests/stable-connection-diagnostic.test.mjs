import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { expect } from '@playwright/test';
import playwrightUtil from '../node_modules/playwright/lib/util.js';
import playwrightExpect from '../node_modules/playwright/lib/matchers/expect.js';
import { buildStableConnectionDiagnostic, redactStableConnectionAssertionError, withoutReportedExpectStep } from '../../tools/e2e-capture/lib/stable-connection-diagnostic.mjs';

const { serializeError } = playwrightUtil;

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
        { at: 75, event: 'connect-start' }],
      preview: 'private_preview',
    },
    stateError: new Error('private_error_text'),
  });
  assert.equal(report.workerChangedDuringStep, true);
  assert.equal(report.stateError, 'Error');
  assert.equal(report.extension.connectionEvents[0].reason, 'other');
  assert.deepEqual(report.extension.connectionEvents.map((entry) => entry.event), ['channel-close', 'connect-start']);
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
