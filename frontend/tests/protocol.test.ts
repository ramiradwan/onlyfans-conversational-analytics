import { readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  isAgentConfigDocumentResponse,
  isAgentConfigGetRequest,
  isAgentToBrainMessage,
  isBrainToAgentMessage,
  isBrainToBridgeMessage,
  isBridgeToBrainMessage,
} from '../src/protocol';

const fixtureRoot = resolve(process.cwd(), '..', 'shared', 'fixtures', 'protocol', 'v2');

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
const bridgeToBrain = new Set(['bridge.hello', 'state.resync']);
const brainToBridge = new Set([
  'bridge.session',
  'state.snapshot',
  'state.delta',
  'presence.state',
  'agent.state',
  'system.state',
]);

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, 'utf8')) as unknown;
}

function validatesOperation(operation: string, value: unknown): boolean {
  if (agentToBrain.has(operation)) return isAgentToBrainMessage(value);
  if (brainToAgent.has(operation)) return isBrainToAgentMessage(value);
  if (bridgeToBrain.has(operation)) return isBridgeToBrainMessage(value);
  if (brainToBridge.has(operation)) return isBrainToBridgeMessage(value);
  if (operation === 'agent.config.get') return isAgentConfigGetRequest(value);
  if (operation === 'agent.config.document') return isAgentConfigDocumentResponse(value);
  throw new Error(`Unmapped operation fixture: ${operation}`);
}

describe('protocol v2 golden fixtures', () => {
  const validFixtures = readdirSync(fixtureRoot).filter((name) => name.endsWith('.json')).sort();

  it('contains and validates one fixture for every matrix operation', () => {
    expect(validFixtures).toHaveLength(25);
    for (const fixture of validFixtures) {
      const operation = fixture.slice(0, -'.json'.length);
      expect(validatesOperation(operation, readJson(`${fixtureRoot}/${fixture}`)), fixture).toBe(true);
    }
  });

  it('accepts protocol.error for both Agent and Bridge recipients', () => {
    const value = readJson(`${fixtureRoot}/protocol.error.json`);
    expect(isBrainToAgentMessage(value)).toBe(true);
    expect(isBrainToBridgeMessage(value)).toBe(true);
  });

  it('rejects each invalid fixture through its directional guard', () => {
    const invalidRoot = `${fixtureRoot}/invalid`;
    const invalidGuards: Record<string, (value: unknown) => boolean> = {
      'empty-consent.agent.config.document.json': isAgentConfigDocumentResponse,
      'malformed-discriminator.unknown-command.json': isBrainToAgentMessage,
      'missing-authorization.agent.config.document.json': isAgentConfigDocumentResponse,
      'missing-identity.ingest.delta.json': isAgentToBrainMessage,
      'missing-resume.agent.session.json': isBrainToAgentMessage,
      'missing-window.agent.config.document.json': isAgentConfigDocumentResponse,
      'unknown-extra.bridge.hello.json': isBridgeToBrainMessage,
      'wrong-enum.agent.state.json': isBrainToBridgeMessage,
      'wrong-type.state.snapshot.json': isBrainToBridgeMessage,
    };
    expect(readdirSync(invalidRoot).filter((name) => name.endsWith('.json'))).toHaveLength(9);
    for (const [fixture, guard] of Object.entries(invalidGuards)) {
      expect(guard(readJson(`${invalidRoot}/${fixture}`)), fixture).toBe(false);
    }
  });
});
