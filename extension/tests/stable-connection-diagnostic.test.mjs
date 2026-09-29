import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { buildStableConnectionDiagnostic } from '../../tools/e2e-capture/lib/stable-connection-diagnostic.mjs';

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
  assert.match(step, /test\.info\(\)\.attach\(/u);
  assert.doesNotMatch(step, /throw new Error\(`Stable connection diagnostic:/u);
});
