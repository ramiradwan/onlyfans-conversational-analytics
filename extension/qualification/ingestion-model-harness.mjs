#!/usr/bin/env node
/* JSON-lines test boundary around the production outbox; contains no delivery rules. */
import readline from 'node:readline';
import { readFile } from 'node:fs/promises';
import { DurableIngestOutbox, INGESTION_STORES } from '../transport/durable-outbox.mjs';
import { createIndexedDbIngestionStorage } from '../transport/indexeddb-ingestion-storage.mjs';
import { FakeIndexedDb } from '../tests/fake-indexeddb.mjs';
import { AgentWebSocketClient } from '../transport/agent-websocket.mjs';

const ACCOUNT = 'qualification-account';
const { version: extensionVersion } = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'));
const KEY = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';
const stores = [INGESTION_STORES.outbox, INGESTION_STORES.chats, INGESTION_STORES.messages,
  INGESTION_STORES.coverageEvidence, INGESTION_STORES.snapshotManifests,
  INGESTION_STORES.snapshotChunks, INGESTION_STORES.snapshotOverrides];
const indexedDb = new FakeIndexedDb();
let serial = 0;
const id = () => `70000000-0000-4000-8000-${String(++serial).padStart(12, '0')}`;
let outbox;
let transport = { connected: false, fence: null, sync_required: false, replay: [], frames: [], connection_id: null };
let client = null; let socket = null;
let transportErrors = [];
let scheduledCallbacks = [];
class Socket { constructor() { this.readyState=0; this.sent=[]; } send(value) { this.sent.push(JSON.parse(value)); } open() { this.readyState=1; this.onopen?.(); } receive(value) { this.onmessage?.({data:JSON.stringify(value)}); } close() { this.readyState=3; this.onclose?.(); } }
const tick = () => new Promise((resolve) => setImmediate(resolve));
function session(fence, committed, resumeAction='resume', pendingSnapshotId=null, nextExpectedChunkIndex=0) { const identity=outbox.identityState(); return { type:'agent.session', protocol_version:'2', message_id:id(), payload:{ connection_id:`60000000-0000-4000-8000-${String(serial + 1).padStart(12, '0')}`, fencing_token:fence, creator_account_id:ACCOUNT, agent_installation_id:'50000000-0000-4000-8000-000000000001', agent_stream_id:identity.agent_stream_id, committed_source_seq:committed, resume_action:resumeAction, required_config_revision:'config-1', reconnect_auth_ticket:'reconnect', config_auth_ticket:'config', pending_snapshot_id:pendingSnapshotId, next_expected_chunk_index:nextExpectedChunkIndex, lease:{heartbeat_interval_seconds:60,lease_timeout_seconds:120} } }; }
function observedFrames() {
  return socket.sent.filter((frame) => frame.type === 'ingest.delta' || frame.type === 'ingest.snapshot').map((frame) => ({
    type: frame.type,
    payload: structuredClone(frame.payload),
  }));
}
async function connect(fence, committed, resumeAction='resume', pendingSnapshotId=null, nextExpectedChunkIndex=0) {
  const identity=outbox.identityState();
  // Each qualification `connect` models a newly constructed worker/client.
  // Stop the previous client before opening the next session.
  client?.stop();
  transportErrors=[];
  client=new AgentWebSocketClient({ extensionVersion, creatorAccountId:ACCOUNT, authTicket:'ticket', outbox,
    identity:{agentInstallationId:'50000000-0000-4000-8000-000000000001',agentStreamId:identity.agent_stream_id,lastAcknowledgedSourceSeq:identity.acknowledged_source_seq,appliedConfigRevision:'config-1'},
    idFactory:id, webSocketFactory:()=>{ socket=new Socket(); return socket; }, scheduler:{setTimeout:(callback)=>{ scheduledCallbacks.push(callback); return callback; },clearTimeout:(callback)=>{ scheduledCallbacks=scheduledCallbacks.filter((item)=>item!==callback); },setInterval:()=>null,clearInterval:()=>{}},
    persistReconnectAuthTicket:async()=>{}, onValidationError:(error)=>transportErrors.push(error.message) });
  const sessionCommitted=committed ?? identity.acknowledged_source_seq;
  client.start(); socket.open(); socket.receive(session(fence, sessionCommitted, resumeAction, pendingSnapshotId, nextExpectedChunkIndex));
  for (let turn=0; turn<20; turn+=1) {
    if (transportErrors.length) throw new Error(`AgentWebSocketClient rejected session: ${transportErrors.join('; ')}`);
    if (resumeAction !== 'snapshot_required' || observedFrames().some((frame) => frame.type === 'ingest.snapshot')) break;
    await new Promise((resolve)=>setTimeout(resolve,0));
  }
  await client.flushOutbox();
  transport={connected:client.session!==null,fence, sync_required:client.syncRequired,replay:socket.sent.filter((x)=>x.type==='ingest.delta').map((x)=>x.payload.source_seq),frames:observedFrames(),connection_id:client.session?.connection_id ?? null};
  return transport;
}
async function reconnectSameClient(fence, committed) {
  if (!client?.session || !socket) throw new Error('A live Agent session is required');
  socket.close();
  const callback=scheduledCallbacks.shift();
  if (callback) callback();
  if (!socket || socket.readyState !== 0) throw new Error('Scheduled reconnect did not open a socket');
  socket.open(); socket.receive(session(fence, committed ?? outbox.identityState().acknowledged_source_seq));
  await tick(); await client.flushOutbox();
  transport={connected:client.session!==null,fence,sync_required:client.syncRequired,replay:socket.sent.filter((x)=>x.type==='ingest.delta').map((x)=>x.payload.source_seq),frames:observedFrames(),connection_id:client.session?.connection_id ?? null,scheduled_callbacks:scheduledCallbacks.length};
  return transport;
}

async function start() {
  outbox = new DurableIngestOutbox({
    storage: createIndexedDbIngestionStorage(indexedDb, { creatorAccountId: ACCOUNT, databaseName: 'qualification', encryptionKey: KEY }),
    creatorAccountId: ACCOUNT, idFactory: id,
  });
  await outbox.initialize();
}
async function state() {
  const values = await outbox.storage.runTransaction('readonly', stores, async (tx) => Object.fromEntries(
    await Promise.all(stores.map(async (name) => [name, (await tx.getAll(name)).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)))])),
  ));
  return { identity: outbox.identityState(), outbox: values[INGESTION_STORES.outbox].sort((a,b) => a.source_seq-b.source_seq),
    chats: values[INGESTION_STORES.chats], messages: values[INGESTION_STORES.messages],
    coverage: values[INGESTION_STORES.coverageEvidence], manifests: values[INGESTION_STORES.snapshotManifests],
    chunks: values[INGESTION_STORES.snapshotChunks], overrides: values[INGESTION_STORES.snapshotOverrides], transport: structuredClone(transport) };
}
async function run(command) {
  switch (command.operation) {
    case 'capture': return await outbox.enqueue(command.change, command.event_id ?? id(), command.origin ?? 'passive');
    case 'capture_message_parent': {
      // Supply stable IDs so replay can compare complete delta envelopes.
      const supplied = Array.isArray(command.event_ids) ? [...command.event_ids] : null;
      if (supplied === null) return await outbox.enqueueMessageWithParent(command.message, command.parent, command.origin ?? 'passive');
      const originalFactory = outbox.idFactory;
      outbox.idFactory = () => supplied.shift() ?? originalFactory();
      try { return await outbox.enqueueMessageWithParent(command.message, command.parent, command.origin ?? 'passive'); }
      finally { outbox.idFactory = originalFactory; }
    }
    case 'ack': {
      if (!client?.session) return await outbox.acknowledge(command.committed_source_seq, command.snapshot_id ?? null, command.snapshot_progress ?? null);
      transportErrors=[];
      const pendingBefore=outbox.identityState().pending_snapshot;
      const frameCountBefore=observedFrames().length;
      socket.receive({type:'ingest.ack',protocol_version:'2',message_id:id(),payload:{connection_id:client.session.connection_id,creator_account_id:ACCOUNT,agent_stream_id:outbox.identityState().agent_stream_id,snapshot_id:command.snapshot_id??null,committed_source_seq:command.committed_source_seq,snapshot_progress:command.snapshot_progress??null}});
      // Wait for the asynchronous acknowledgement transaction.
      for (let turn=0; turn<20; turn+=1) {
        const durable=outbox.identityState().acknowledged_source_seq>=command.committed_source_seq;
        const trimmed=(await outbox.entries()).every((item)=>item.source_seq>command.committed_source_seq);
        const pending=outbox.identityState().pending_snapshot;
        const progressSettled=command.snapshot_progress == null
          || (command.snapshot_progress.committed ? pending === null : pending?.next_expected_chunk_index === command.snapshot_progress.next_expected_chunk_index);
        const emittedRequiredFrame=command.snapshot_progress == null || command.snapshot_progress.committed || observedFrames().length > frameCountBefore;
        if (durable && trimmed && progressSettled && emittedRequiredFrame) break;
        await new Promise((resolve)=>setTimeout(resolve,0));
      }
      if (transportErrors.length) throw new Error(`AgentWebSocketClient rejected ACK: ${transportErrors.join('; ')}`);
      transport={...transport,sync_required:client.syncRequired,frames:observedFrames(),connection_id:client.session.connection_id};
      return { committedSourceSeq:outbox.identityState().acknowledged_source_seq,
        snapshotAcknowledged:Boolean(command.snapshot_progress?.committed && pendingBefore?.snapshot_id === command.snapshot_id && outbox.identityState().pending_snapshot === null) };
    }
    case 'snapshot_create': return await outbox.createSnapshot(command.snapshot_id ?? id());
    case 'snapshot_build': return await outbox.buildNextSnapshotChunk();
    case 'snapshot_prepare': return await outbox.prepareSnapshot(command.snapshot_id ?? id());
    case 'restart': client?.stop(); client=null; socket=null; transportErrors=[]; scheduledCallbacks=[]; transport={connected:false,fence:null,sync_required:false,replay:[],frames:[],connection_id:null,scheduled_callbacks:0}; await start(); return outbox.identityState();
    // Observe transport state separately from protocol rejection.
    case 'connect': return await connect(command.fence, command.committed_source_seq, command.resume_action ?? 'resume', command.pending_snapshot_id ?? null, command.next_expected_chunk_index ?? 0);
    case 'same_client_reconnect': return await reconnectSameClient(command.fence, command.committed_source_seq);
    case 'disconnect': socket?.close(); transport = { ...transport, connected: false }; return transport;
    case 'sync_required': {
      if (!client?.session) throw new Error('A live Agent session is required');
      const snapshotFrameCountBefore=observedFrames().filter((frame) => frame.type === 'ingest.snapshot').length;
      socket.receive({type:'sync.required',protocol_version:'2',message_id:id(),payload:{connection_id:client.session.connection_id,creator_account_id:ACCOUNT,reason:'sequence_gap',expected_agent_stream_id:outbox.identityState().agent_stream_id,expected_next_source_seq:outbox.identityState().acknowledged_source_seq+1,pending_snapshot_id:command.pending_snapshot_id ?? null,next_expected_chunk_index:command.next_expected_chunk_index ?? 0,snapshot:{include_chats:true,include_messages:true,include_coverage_evidence:true,max_frame_bytes:524288,max_records_per_chunk:100}}});
      for (let turn=0; turn<20; turn+=1) {
        if (transportErrors.length) throw new Error(`AgentWebSocketClient rejected sync.required: ${transportErrors.join('; ')}`);
        if (observedFrames().filter((frame) => frame.type === 'ingest.snapshot').length > snapshotFrameCountBefore) break;
        await new Promise((resolve)=>setTimeout(resolve,0));
      }
      transport={...transport,sync_required:client.syncRequired,frames:observedFrames(),connection_id:client.session.connection_id}; return transport;
    }
    case 'sync_resolved': transport = { ...transport, sync_required: false }; return transport;
    case 'read': return null;
    default: throw new Error(`Unknown qualification operation ${command.operation}`);
  }
}
await start();
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  if (!line) continue;
  const command = JSON.parse(line);
  try { process.stdout.write(`${JSON.stringify({ ok: true, result: await run(command), state: await state() })}\n`); }
  catch (error) { process.stdout.write(`${JSON.stringify({ ok: false, error: { code: error.code ?? 'invariant_failed', message: error.message }, state: await state() })}\n`); }
}
