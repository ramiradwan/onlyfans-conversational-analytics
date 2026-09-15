// QA source entry point only. Never included in a shipping extension build.
import { createSignerReleaseFixture } from '../tests/signer-release-fixture.mjs';
import { TRAVERSAL_ACCOUNT, TRAVERSAL_TIME, TRAVERSAL_KEY,
  nativeTraversalBody, traversalConfiguration } from '../tests/signer-traversal-scenario.mjs';
import { createReadOnlyIndexedDbIngestionStorage } from '../transport/read-only-indexeddb-ingestion-storage.mjs';
import { DurableIngestOutbox } from '../transport/read-only-durable-outbox.mjs';
import { HistoryAcquisitionCoordinator } from '../transport/read-only-history-coordinator.mjs';
import { createAccountSigningPersistence } from '../transport/agent-runtime-core.mjs';

const realStorage = createReadOnlyIndexedDbIngestionStorage(indexedDB, {
  databaseName: 'synthetic-signer-worker-recovery', encryptionKey: TRAVERSAL_KEY,
});
let pausePageCommit = false;
let enteredPageCommit = false;
const cursors = [];
const f = createSignerReleaseFixture();
f.reply = (request) => f.response(nativeTraversalBody(request));
const storage = {
  runTransaction(mode, stores, work, controls) {
    return realStorage.runTransaction(mode, stores, async (tx) => {
      const result = await work(tx);
      if (pausePageCommit && enteredPageCommit && mode === 'readwrite') {
        // Production writes have entered real IDB but its transaction has not committed.
        // The production encrypted adapter keeps the transaction alive while suspended.
        postMessage({ stage: 'page-transaction-entered', cursors, reloads: f.calls.reloads });
        await new Promise(() => {});
      }
      return result;
    }, controls);
  },
};
const outbox = new DurableIngestOutbox({ storage, creatorAccountId: TRAVERSAL_ACCOUNT });
const commitPage = outbox.commitPage.bind(outbox);
outbox.commitPage = async (options) => {
  enteredPageCommit = true;
  try { return await commitPage(options); }
  finally { enteredPageCommit = false; }
};
f.persistence = createAccountSigningPersistence(realStorage, TRAVERSAL_ACCOUNT);
let coordinator;

async function snapshot() {
  const jobs = await outbox.historyJobs();
  const material = await realStorage.runTransaction('readonly',
    ['chats', 'messages', 'coverage_evidence', 'outbox'], async (tx) => ({
      chats: (await tx.getAll('chats')).sort((a, b) => a.chat_id.localeCompare(b.chat_id)),
      messages: (await tx.getAll('messages')).sort((a, b) => a.message_id.localeCompare(b.message_id)),
      evidence: (await tx.getAll('coverage_evidence')).sort((a, b) => a.source_seq - b.source_seq),
      outbox: (await tx.getAll('outbox')).sort((a, b) => a.source_seq - b.source_seq),
    }));
  return { identity: outbox.identityState(), jobs, ...material, cursors, reloads: f.calls.reloads };
}

onmessage = async ({ data }) => {
  try {
    await outbox.initialize();
    if (data.action === 'cancel-after-callback') {
      const controller = new AbortController();
      let callbackReturned = false;
      let rejected = false;
      try {
        await realStorage.runTransaction('readwrite', ['config'], async (tx) => {
          await tx.put('config', { key: 'synthetic-cancel-probe', value: 'synthetic-staged' });
          callbackReturned = true;
        }, { signal: controller.signal, assertCurrent() {
          // The adapter calls its final guard after work returns, before native completion.
          if (callbackReturned) controller.abort('synthetic-cancel-after-callback');
        } });
      } catch { rejected = true; }
      const record = await realStorage.runTransaction('readonly', ['config'],
        (tx) => tx.get('config', 'synthetic-cancel-probe'));
      if (!callbackReturned || !controller.signal.aborted || !rejected || record !== undefined) {
        throw new Error('Cancellation after callback did not roll back');
      }
      postMessage({ stage: 'cancel-after-callback-rolled-back' });
      return;
    }
    if (data.action === 'snapshot') {
      postMessage({ stage: 'snapshot', snapshot: await snapshot() });
      return;
    }
    if (data.action !== 'wake') throw new Error('Unknown QA action');
    const provider = await f.createProvider();
    coordinator = new HistoryAcquisitionCoordinator({
      outbox,
      signer: { async read(request) {
        cursors.push({ operation: request.operation, conversationId: request.parameters?.conversationId,
          cursor: request.parameters?.query?.cursor });
        return provider.read(request);
      } },
      configuration: () => traversalConfiguration(),
      session: () => ({ creator_account_id: TRAVERSAL_ACCOUNT, applied_config_revision: 'synthetic-config-v1' }),
      now: () => TRAVERSAL_TIME,
    });
    pausePageCommit = data.pausePageCommit === true;
    const result = await coordinator.wake();
    postMessage({ stage: 'committed', result, snapshot: await snapshot() });
  } catch (error) {
    // Only fixed error identity crosses the QA worker boundary; no response or signer state.
    postMessage({ stage: 'failed', error: { name: error?.name ?? 'UnknownError',
      code: error?.code ?? null } });
  }
};
