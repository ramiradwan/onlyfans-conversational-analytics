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

const fixtureRoot = fileURLToPath(new URL('../../shared/fixtures/protocol/v1/', import.meta.url));
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
  assert.equal(fixtures.length, 5);

  for (const fixture of fixtures) {
    const value = readJson(`${invalidRoot}/${fixture}`);
    const accepted = isAgentToBrainMessage(value)
      || isBrainToAgentMessage(value)
      || isAgentConfigGetRequest(value)
      || isAgentConfigDocumentResponse(value);
    assert.equal(accepted, false, fixture);
  }
});
