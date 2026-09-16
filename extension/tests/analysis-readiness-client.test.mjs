import assert from 'node:assert/strict';
import test from 'node:test';

import { createCompanionClient } from '../runtime/companion-client.mjs';

const ACCOUNT = 'creator-1';
const STORAGE_KEY = Buffer.alloc(32, 7).toString('base64');
const CHALLENGE = Buffer.alloc(32, 9).toString('base64url');

function area() {
  const values = {};
  return {
    values,
    async get(keys) {
      return Object.fromEntries(keys
        .filter((key) => Object.hasOwn(values, key))
        .map((key) => [key, values[key]]));
    },
    async set(update) { Object.assign(values, structuredClone(update)); },
    async remove(keys) { for (const key of keys) delete values[key]; },
  };
}

async function harness(readinessDocument) {
  const keypair = await crypto.subtle.generateKey(
    { name: 'ECDSA', namedCurve: 'P-256' },
    false,
    ['sign', 'verify'],
  );
  const chromeApi = {
    runtime: { getURL: (path) => `chrome-extension://${'a'.repeat(32)}/${path}` },
    storage: { local: area(), session: area() },
  };
  const methods = [];
  const closeListeners = [];
  const channel = {
    identity: {
      creator_account_id: ACCOUNT,
      pairing_id: 'A'.repeat(43),
      installation_id: 'brain.installation',
    },
    closed: false,
    close() {
      if (this.closed) return;
      this.closed = true;
      for (const listener of closeListeners) listener();
    },
    onClose(listener) { closeListeners.push(listener); },
    onMessage() { return () => {}; },
    async rpc(method) {
      methods.push(method);
      if (method === 'agent.challenge') {
        return {
          challenge_id: crypto.randomUUID(),
          challenge: CHALLENGE,
          session_id: crypto.randomUUID(),
          expires_at: '2026-09-15T22:30:00Z',
        };
      }
      if (method === 'agent.authenticate') {
        return {
          creator_account_id: ACCOUNT,
          auth_ticket: 'fresh-auth-ticket',
          storage_bootstrap: 'sealed-bootstrap',
        };
      }
      if (method === 'agent.storage.unseal') {
        return {
          schema: 'ofca-extension-storage-unlock/v1',
          creator_account_id: ACCOUNT,
          credential_kind: 'pairing',
          auth_ticket: 'fresh-auth-ticket',
          storage_key_base64: STORAGE_KEY,
        };
      }
      if (method === 'agent.analysis.readiness') return readinessDocument;
      throw new Error(`unexpected RPC ${method}`);
    },
  };
  const pairingStore = {
    async status() { return { paired: true }; },
    async identity() { return { privateKey: keypair.privateKey }; },
    close() {},
  };
  const client = createCompanionClient({
    chromeApi,
    allowsFull: () => true,
    detectedAccountId: async () => ACCOUNT,
    accountDatabaseName: async (accountId) => `account-${accountId}`,
    storeFactory: async () => pairingStore,
    loadSnow: async () => ({ SnowSession: class {}, generateStaticKeypair() { return new Uint8Array(64); } }),
    loadTrust: async () => ({}),
    channelFactory: async () => channel,
  });
  return { client, methods };
}

test('analysis readiness is read only over the authenticated companion channel', async () => {
  const { client, methods } = await harness({
    schema: 'ofca-analysis-readiness/v1',
    commercial_authority: 'active',
    analysis_admission: 'admitted',
  });
  try {
    assert.deepEqual(await client.analysisReadiness(), {
      commercial_authority: 'active',
      analysis_admission: 'admitted',
    });
    assert.deepEqual(methods, [
      'agent.challenge',
      'agent.authenticate',
      'agent.storage.unseal',
      'agent.analysis.readiness',
    ]);
  } finally {
    client.invalidate();
  }
});

test('analysis readiness rejects malformed and impossible ready responses', async () => {
  const invalid = [
    {
      schema: 'ofca-analysis-readiness/v1',
      commercial_authority: 'required',
      analysis_admission: 'admitted',
    },
    {
      schema: 'ofca-analysis-readiness/v1',
      commercial_authority: 'active',
      analysis_admission: 'admitted',
      license_id: 'must-not-be-exposed',
    },
    {
      schema: 'ofca-analysis-readiness/v2',
      commercial_authority: 'active',
      analysis_admission: 'admitted',
    },
  ];
  for (const document of invalid) {
    const { client } = await harness(document);
    try {
      await assert.rejects(client.analysisReadiness(), { message: 'companion_session_refused' });
    } finally {
      client.invalidate();
    }
  }
});
