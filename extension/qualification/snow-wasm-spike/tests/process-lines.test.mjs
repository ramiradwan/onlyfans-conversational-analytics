import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import test from 'node:test';
import { createLineQueue, stopChild } from '../process-lines.mjs';

test('child line queue preserves lines emitted before the second read', async () => {
  const child = spawn(process.execPath, ['-e', "process.stdout.write('ready\\napplication-secret-observed=false\\n'); setTimeout(() => {}, 1000)"], { stdio: ['ignore', 'pipe', 'inherit'] });
  const lines = createLineQueue(child, 'fixture');
  assert.equal(await lines.nextLine(1000), 'ready');
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(await lines.nextLine(1000), 'application-secret-observed=false');
  await stopChild(child, 'fixture');
});
