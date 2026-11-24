import { createCompanionClient } from '../runtime/companion-client.mjs';
import { ConsentController } from '../runtime/consent-controller.mjs';
import { LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE } from '../runtime/legal-activation-controller.mjs';
import { openCompanionChannel } from '../transport/companion-channel.mjs';
import { loadGrantTrustSet } from '../transport/grant-verifier.mjs';

let client;
let paired = null;
let pairFailure = false;
let socket;
let session;
let burst;
let consent;
const readinessReads = [];

async function qualificationChannel(options) {
  const channel = await openCompanionChannel(options);
  return { ...channel, get closed() { return channel.closed; },
    async rpc(method, ...args) {
      const result = await channel.rpc(method, ...args);
      if (!consent || method !== 'agent.analysis.readiness') return result;
      // Hold the verified native reply at the worker boundary. The encrypted
      // request and authorization have already run through Brain.
      const hold = Promise.withResolvers();
      readinessReads.push(hold.resolve);
      await hold.promise;
      return result;
    },
  };
}

export async function startReadiness() {
  consent = new ConsentController({
    // The fixture authorizes a synthetic choice. No platform tab is created.
    chromeApi: { storage: chrome.storage, runtime: chrome.runtime, tabs: { query: async () => [] },
      permissions: { contains: async () => true }, scripting: {
        getRegisteredContentScripts: async () => [], registerContentScripts: async () => {},
      } },
    runtime: { start: async () => {}, suspend: async () => {} }, adapter: client.adapter,
    provisioningIdentityBridge: { register() {}, clearContexts: async () => {} },
    previewMetrics: { record() {}, summary: async () => ({}), clear() {}, prune: async () => {} },
    clearLocalData: async () => {},
    activeModeAuthorization: {
      authorizeTransition: async () => true, authorizeResume: async () => true, reconcileActiveMode: async () => true,
    },
    runtimeSummary: () => ({ transport_state: client.connected ? 'authenticated' : 'disconnected' }),
    fetchImpl: async () => new Response(null, { status: 200 }),
  });
  await advanceReadinessConsent();
  client.registerPopup();
  consent.subscribe(client.notifySurfaces);
  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (message?.type === LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE) respond({ ok: true, result: {} });
  });
}

export async function advanceReadinessConsent() {
  const epoch = consent.state.consent_epoch;
  const next = await consent.runLegalOperation(({ setMode }) =>
    setMode('full', { evidenceEventId: crypto.randomUUID() }));
  return next.consent.mode === 'full' && next.consent.consent_epoch !== epoch;
}

export function readinessCount() { return readinessReads.length; }
export function releaseReadiness(index) { readinessReads[index](); }


async function snapshotBurst() {
  const identity = {
    connection_id: session.connection_id, fencing_token: session.fencing_token,
    creator_account_id: session.creator_account_id, agent_installation_id: session.agent_installation_id,
    agent_stream_id: session.agent_stream_id, snapshot_id: crypto.randomUUID(),
  };
  const envelope = (payload) => ({ type: 'ingest.snapshot', protocol_version: '2',
    message_id: crypto.randomUUID(), payload: { ...identity, ...payload } });
  const acknowledged = (value) => new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('production_snapshot_timeout')), 10_000);
    socket.onclose = () => { clearTimeout(timeout); reject(new Error('production_snapshot_closed')); };
    socket.onmessage = ({ data }) => {
      const response = JSON.parse(data);
      if (response.correlation_id !== value.message_id) return;
      clearTimeout(timeout);
      if (response.type !== 'ingest.ack') reject(new Error('production_snapshot_refused'));
      else resolve(response.payload);
    };
    socket.send(JSON.stringify(value));
  });
  await acknowledged(envelope({ frame_kind: 'begin', through_seq: 0, chunk_count: 2,
    record_counts: { chats: 1, messages: 2, coverage_evidence: 0 }, max_frame_bytes: 524_288 }));
  await acknowledged(envelope({ frame_kind: 'chunk', chunk_index: 0, entity_kind: 'chat', records: [{
    tombstone: false, chat: { record_kind: 'full', chat_id: 'qualification-chat', platform_user_id: 'qualification-peer',
      display_name: 'Qualification fixture', updated_at: '2026-08-29T09:00:00Z' },
  }] }));
  const large = envelope({ frame_kind: 'chunk', chunk_index: 1, entity_kind: 'message', records: [0, 1].map((index) => ({
    tombstone: false, message: { message_id: `qualification-message-${index}`, chat_id: 'qualification-chat',
      sender_platform_user_id: 'qualification-peer', text: '', sent_at: '2026-08-29T09:00:00Z', direction: 'inbound' },
  })) });
  const padding = 524_288 - new TextEncoder().encode(JSON.stringify(large)).length;
  large.payload.records[0].message.text = 'x'.repeat(Math.floor(padding / 2));
  large.payload.records[1].message.text = 'x'.repeat(padding - Math.floor(padding / 2));
  if (new TextEncoder().encode(JSON.stringify(large)).length !== 524_288) throw new Error('qualification_size_invalid');
  await acknowledged(large);
  const committed = await acknowledged(envelope({ frame_kind: 'commit', chunk_count: 2 }));
  if (committed.snapshot_progress?.committed !== true) throw new Error('production_snapshot_not_committed');
}

export async function prepare({ account, trust, now }) {
  // The signed, reconstructable contract fixtures have a declared verifier
  // clock. This assignment exists only in the temporary qualification worker.
  Date.now = () => now * 1000;
  const verifiedTrust = await loadGrantTrustSet(trust, { allowNonProduction: true });
  client = createCompanionClient({
    allowsFull: () => !consent || consent.state.mode === 'full',
    readinessConsentEpoch: () => consent?.state.consent_epoch ?? null,
    channelFactory: qualificationChannel,
    detectedAccountId: async () => account,
    accountDatabaseName: async () => 'qualification-account',
    loadTrust: async () => verifiedTrust,
  });
  return { prepared: true };
}

export function beginPairing() {
  paired = client.pair().then(() => true).catch(() => { pairFailure = true; return false; });
  return { started: true };
}

export async function comparison() {
  const state = await client.status();
  return { state: state.state, comparison_code: state.comparison_code, failed: pairFailure };
}

export async function finishPairing() {
  if (!await paired || !client.connected) throw new Error('production_pairing_failed');
  return { paired: true };
}

export async function exercise() {
  const binding = await client.adapter.loadBrainBinding();
  if (typeof binding.storageKey !== 'string' || atob(binding.storageKey).length !== 32) {
    throw new Error('production_storage_key_invalid');
  }
  const account = binding.creatorAccountId;
  const installation = await client.adapter.loadAgentInstallationId();
  socket = client.webSocketFactory();
  session = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('production_hello_timeout')), 10_000);
    socket.onclose = () => { clearTimeout(timeout); reject(new Error('production_hello_refused')); };
    socket.onmessage = ({ data }) => {
      const value = JSON.parse(data);
      if (value.type === 'agent.session') { clearTimeout(timeout); resolve(value.payload); }
    };
    socket.onopen = () => socket.send(JSON.stringify({
      type: 'agent.hello', protocol_version: '2', message_id: crypto.randomUUID(),
      payload: {
        auth_ticket: socket.authTicket, agent_installation_id: installation,
        requested_creator_account_id: account, capabilities: ['capture.chats'], extension_version: '2.0.3',
        agent_stream_id: crypto.randomUUID(), last_acknowledged_source_seq: 0, applied_config_revision: null,
      },
    }));
  });
  const config = await client.configAdapter.fetchConfig({
    creatorAccountId: account, authTicket: session.config_auth_ticket,
    agentInstallationId: installation, currentEtag: null, currentConfigRevision: null,
    supportedSchemaVersions: ['2'],
  });
  if (config.status !== 200) throw new Error('production_config_refused');
  await client.adapter.saveReconnectAuthTicket({
    creatorAccountId: account, agentInstallationId: installation,
    authTicket: session.reconnect_auth_ticket, configAuthTicket: session.config_auth_ticket,
  });
  await snapshotBurst();
  return { authenticated: true, storage_unsealed: true, protocol_v2: true, config: true, rotated: true,
    snapshot_512k_committed: true };
}

export async function revoked() {
  const deadline = performance.now() + 5000;
  while (client.connected && performance.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 20));
  const closed = !client.connected;
  let refused = false;
  try { await client.adapter.loadBrainBinding(); } catch { refused = true; }
  return { closed, refused };
}

export function listenForBurst() {
  burst = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('production_inbound_burst_timeout')), 10_000);
    socket.onclose = () => { clearTimeout(timeout); reject(new Error('production_inbound_burst_closed')); };
    socket.onmessage = ({ data }) => {
      const value = JSON.parse(data);
      if (value.type !== 'command.execute') return;
      clearTimeout(timeout);
      if (new TextEncoder().encode(data).length !== 524_288 || !/^x+$/u.test(value.payload.action.text)) {
        reject(new Error('production_inbound_burst_invalid'));
      } else resolve({ inbound_512k: true });
    };
  });
  void burst.catch(() => {});
  return { listening: true };
}

export async function receiveBurst() { return burst; }
