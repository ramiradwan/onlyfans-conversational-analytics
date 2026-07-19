import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import {
  isAgentConfigDocumentResponse,
  isAgentConfigGetRequest,
  isAgentToBrainMessage,
  isBrainToAgentMessage,
} from '../protocol/index.mjs';

const fixtureRoot = fileURLToPath(new URL('../../shared/fixtures/protocol/v2/', import.meta.url));
const agentToBrain = new Set([
  'agent.hello',
  'agent.heartbeat',
  'ingest.snapshot',
  'ingest.delta',
  'presence.observed',
  'config.applied',
  'command.result',
]);
const brainToAgent = new Set([
  'agent.session',
  'sync.required',
  'ingest.ack',
  'ingest.rejected',
  'protocol.error',
  'config.available',
  'command.execute',
  'command.result.ack',
]);

function readJson(path) {
  return JSON.parse(readFileSync(path, 'utf8'));
}

function validatesAgentOperation(operation, value) {
  if (agentToBrain.has(operation)) return isAgentToBrainMessage(value);
  if (brainToAgent.has(operation)) return isBrainToAgentMessage(value);
  if (operation === 'agent.config.get') return isAgentConfigGetRequest(value);
  if (operation === 'agent.config.document') return isAgentConfigDocumentResponse(value);
  return false;
}

test('all 17 Agent-relevant operation fixtures pass dependency-free validation', () => {
  const fixtures = readdirSync(fixtureRoot)
    .filter((name) => name.endsWith('.json'))
    .filter((name) => {
      const operation = name.slice(0, -'.json'.length);
      return agentToBrain.has(operation)
        || brainToAgent.has(operation)
        || operation === 'agent.config.get'
        || operation === 'agent.config.document';
    });

  assert.equal(fixtures.length, 17);
  for (const fixture of fixtures) {
    const operation = fixture.slice(0, -'.json'.length);
    assert.equal(validatesAgentOperation(operation, readJson(`${fixtureRoot}/${fixture}`)), true, fixture);
  }
});

test('all invalid fixtures are rejected by the Agent protocol surface', () => {
  const invalidRoot = `${fixtureRoot}/invalid`;
  const fixtures = readdirSync(invalidRoot).filter((name) => name.endsWith('.json'));
  assert.equal(fixtures.length, 9);

  for (const fixture of fixtures) {
    const value = readJson(`${invalidRoot}/${fixture}`);
    const accepted = isAgentToBrainMessage(value)
      || isBrainToAgentMessage(value)
      || isAgentConfigGetRequest(value)
      || isAgentConfigDocumentResponse(value);
    assert.equal(accepted, false, fixture);
  }
});

test('all snapshot rejection codes are accepted by the Agent protocol surface', () => {
  const document = readJson(`${fixtureRoot}/ingest.rejected.json`);
  for (const code of ['chunk_conflict', 'snapshot_incomplete', 'frame_too_large']) {
    const candidate = structuredClone(document);
    candidate.payload.code = code;
    assert.equal(isBrainToAgentMessage(candidate), true, code);
  }
});

test('snapshot progress has no artificial ten-thousand-chunk ceiling', () => {
  const snapshot = readJson(`${fixtureRoot}/ingest.snapshot.json`);
  snapshot.payload.chunk_count = 10_001;
  assert.equal(isAgentToBrainMessage(snapshot), true);
});

test('history scheduling fields have no arbitrary extension-only ceilings', () => {
  const document = readJson(`${fixtureRoot}/agent.config.document.json`);
  document.history_acquisition.pages_per_wake = 10_001;
  document.history_acquisition.request_interval_ms = 3_600_001;
  document.history_acquisition.retry_limit = 101;
  assert.equal(isAgentConfigDocumentResponse(document), true);
});
