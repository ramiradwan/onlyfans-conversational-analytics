import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { build } from 'esbuild';

import {
  READ_ONLY_CAPABILITIES,
  parseAgentConfigDocumentResponse,
  parseAgentToBrainMessage,
} from '../protocol/read-only.mjs';
import { ReadOnlyAgentWebSocketClient } from '../transport/read-only-agent-websocket.mjs';

const EXTENSION_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STORE_ENTRY = path.join(EXTENSION_ROOT, 'background-read-only.js');
const FORBIDDEN_MODULE_SUFFIXES = Object.freeze([
  '/protocol/index.mjs',
  '/protocol/validation.mjs',
  '/transport/agent-command-service.mjs',
  '/transport/agent-config-client.mjs',
  '/transport/agent-runtime.mjs',
  '/transport/agent-websocket.mjs',
  '/transport/capture-ingestion.mjs',
  '/transport/chrome-adapter.mjs',
  '/transport/config-http-adapter.mjs',
  '/transport/durable-outbox.mjs',
  '/transport/indexeddb-ingestion-storage.mjs',
]);
const FORBIDDEN_SURFACES = Object.freeze([
  /AgentCommandService/,
  /command\.execute/,
  /command\.message\.send/,
  /command\.result/,
  /command_results/,
  /loadCommandState/,
  /message\.send/,
  /onCommand/,
  /saveCommandState/,
]);

async function sourceGraph(entry) {
  const bundle = await build({
    absWorkingDir: EXTENSION_ROOT,
    bundle: true,
    entryPoints: [path.relative(EXTENSION_ROOT, entry)],
    external: ['local-authenticated-read-connector/*'],
    format: 'esm',
    metafile: true,
    platform: 'browser',
    target: ['chrome116'],
    treeShaking: true,
    write: false,
  });
  const graph = new Map();
  for (const input of Object.keys(bundle.metafile.inputs)) {
    const filename = path.resolve(EXTENSION_ROOT, input);
    if (filename.startsWith(EXTENSION_ROOT + path.sep)) {
      graph.set(filename, await readFile(filename, 'utf8'));
    }
  }
  return { graph, bundledSource: bundle.outputFiles[0].text };
}

class FakeSocket {
  constructor() {
    this.readyState = 0;
    this.sent = [];
    this.closed = null;
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  receive(document) {
    this.onmessage?.({ data: JSON.stringify(document) });
  }

  send(value) {
    this.sent.push(JSON.parse(value));
  }

  close(code, reason) {
    this.closed = { code, reason };
    this.readyState = 3;
  }
}

function session() {
  return {
    type: 'agent.session',
    protocol_version: '2',
    message_id: '30000000-0000-4000-8000-000000000001',
    payload: {
      connection_id: '40000000-0000-4000-8000-000000000001',
      fencing_token: 'fence-1',
      creator_account_id: 'creator-1',
      agent_installation_id: '10000000-0000-4000-8000-000000000001',
      agent_stream_id: '20000000-0000-4000-8000-000000000001',
      committed_source_seq: 0,
      resume_action: 'resume',
      required_config_revision: 'config-1',
      reconnect_auth_ticket: 'reconnect-1',
      config_auth_ticket: 'config-auth-1',
      pending_snapshot_id: null,
      next_expected_chunk_index: 0,
      lease: { heartbeat_interval_seconds: 30, lease_timeout_seconds: 90 },
    },
  };
}

test('Store background graph contains only read-only transport and protocol surfaces', async () => {
  const { graph, bundledSource } = await sourceGraph(STORE_ENTRY);
  const relativeNames = [...graph.keys()].map((filename) => (
    `/${path.relative(EXTENSION_ROOT, filename).replaceAll('\\', '/')}`
  ));
  for (const suffix of FORBIDDEN_MODULE_SUFFIXES) {
    assert.deepEqual(
      relativeNames.filter((filename) => filename.endsWith(suffix)),
      [],
      suffix,
    );
  }
  for (const [filename, source] of graph) {
    for (const expression of FORBIDDEN_SURFACES) {
      assert.doesNotMatch(source, expression, path.relative(EXTENSION_ROOT, filename));
    }
  }
  for (const expression of FORBIDDEN_SURFACES) {
    assert.doesNotMatch(bundledSource, expression, 'bundled Store background');
  }
});

test('read-only handshake advertises exactly the four analytics capabilities', () => {
  assert.deepEqual(READ_ONLY_CAPABILITIES, [
    'capture.chats',
    'capture.messages',
    'capture.presence',
    'history.sync',
  ]);
  const sockets = [];
  const validationErrors = [];
  const client = new ReadOnlyAgentWebSocketClient({
    identity: {
      agentInstallationId: '10000000-0000-4000-8000-000000000001',
      agentStreamId: '20000000-0000-4000-8000-000000000001',
      lastAcknowledgedSourceSeq: 0,
      appliedConfigRevision: null,
    },
    creatorAccountId: 'creator-1',
    authTicket: 'bootstrap-1',
    webSocketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    scheduler: {
      setTimeout: () => 1,
      clearTimeout: () => {},
      setInterval: () => 1,
      clearInterval: () => {},
    },
    idFactory: () => '50000000-0000-4000-8000-000000000001',
    onValidationError: (error) => validationErrors.push(error),
  });

  client.start();
  sockets[0].open();
  assert.deepEqual(sockets[0].sent[0].payload.capabilities, READ_ONLY_CAPABILITIES);
  sockets[0].receive(session());
  sockets[0].receive({
    type: 'command.execute',
    protocol_version: '2',
    message_id: '60000000-0000-4000-8000-000000000001',
    payload: {},
  });
  assert.equal(validationErrors.length, 1);
  assert.deepEqual(sockets[0].closed, {
    code: 4002,
    reason: 'Invalid protocol frame from Brain',
  });
  assert.equal(sockets[0].sent.some((message) => message.type.startsWith('command.')), false);
});

test('read-only protocol rejects an added capability before transport', () => {
  assert.throws(() => parseAgentToBrainMessage({
    type: 'agent.hello',
    protocol_version: '2',
    message_id: '50000000-0000-4000-8000-000000000001',
    payload: {
      auth_ticket: 'ticket',
      agent_installation_id: '10000000-0000-4000-8000-000000000001',
      requested_creator_account_id: 'creator-1',
      capabilities: [...READ_ONLY_CAPABILITIES, 'mutation.unsupported'],
      extension_version: '2.0.0',
      agent_stream_id: '20000000-0000-4000-8000-000000000001',
      last_acknowledged_source_seq: 0,
      applied_config_revision: null,
    },
  }), /capabilities/);
});

test('read-only configuration accepts no platform action policy', () => {
  const document = {
    operation: 'agent.config.document',
    protocol_version: '2',
    creator_account_id: 'creator-1',
    config_revision: 'config-1',
    config_schema_version: '2',
    digest: `sha256:${'0'.repeat(64)}`,
    etag: 'config-1',
    issued_at: '2026-08-26T00:00:00Z',
    capture_policy: {
      observation_interval_seconds: 30,
      rules: [{ resource: 'chats', url_pattern: '/api2/v2/chats', enabled: true }],
    },
    command_policy: {
      allowed_actions: [],
      max_text_length: 1,
      require_idempotency: true,
    },
    history_acquisition: {
      enabled: false,
      consent_revision: null,
      authorized_platform_creator_id: null,
      recent_window_days: 30,
      page_size: 50,
      pages_per_wake: 2,
      request_interval_ms: 1_000,
      retry_limit: 3,
    },
  };
  assert.equal(parseAgentConfigDocumentResponse(structuredClone(document)).config_revision, 'config-1');
  document.command_policy.allowed_actions.push('unsupported.action');
  assert.throws(() => parseAgentConfigDocumentResponse(document), /allowed_actions/);
});
