const clone = (value) => structuredClone(value);

export const INGESTION_STORES = Object.freeze({
  meta: 'meta',
  outbox: 'outbox',
  chats: 'chats',
  messages: 'messages',
  snapshot: 'snapshot',
});

export const INGESTION_META_KEY = 'state';

const ALL_STORES = Object.freeze(Object.values(INGESTION_STORES));
const STATE_VERSION = 1;

function emptyState() {
  return {
    version: STATE_VERSION,
    last_source_seq: 0,
    acknowledged_source_seq: 0,
    outbox: [],
    chats: [],
    messages: [],
    pending_snapshot: null,
  };
}

function normalizeState(saved) {
  if (saved === undefined || saved === null) return emptyState();
  if (
    saved.version !== STATE_VERSION ||
    !Number.isSafeInteger(saved.last_source_seq) ||
    saved.last_source_seq < 0 ||
    !Number.isSafeInteger(saved.acknowledged_source_seq) ||
    saved.acknowledged_source_seq < 0 ||
    !Array.isArray(saved.outbox) ||
    !Array.isArray(saved.chats) ||
    !Array.isArray(saved.messages)
  ) {
    throw new Error('Stored durable ingestion state is invalid');
  }
  const state = clone(saved);
  state.pending_snapshot ??= null;
  state.outbox.sort((left, right) => left.source_seq - right.source_seq);
  let previous = state.acknowledged_source_seq;
  for (const item of state.outbox) {
    if (
      typeof item.event_id !== 'string' ||
      !Number.isSafeInteger(item.source_seq) ||
      item.source_seq !== previous + 1 ||
      typeof item.change !== 'object' ||
      item.change === null
    ) {
      throw new Error('Stored durable outbox is not contiguous and ordered');
    }
    previous = item.source_seq;
  }
  if (previous > state.last_source_seq) {
    throw new Error('Stored source sequence is behind the durable outbox');
  }
  return state;
}

function metaFromState(state) {
  const pending = state.pending_snapshot;
  return {
    version: state.version,
    last_source_seq: state.last_source_seq,
    acknowledged_source_seq: state.acknowledged_source_seq,
    pending_snapshot: pending === null
      ? null
      : { snapshot_id: pending.snapshot_id, through_seq: pending.through_seq },
  };
}

function snapshotRecord(kind, id, value) {
  return { key: `${kind}:${id}`, kind, value: clone(value) };
}

function materializedSnapshot(metadata, records) {
  if (metadata === null || metadata === undefined) return null;
  return {
    snapshot_id: metadata.snapshot_id,
    through_seq: metadata.through_seq,
    chats: records
      .filter((record) => record.kind === 'chat')
      .map((record) => clone(record.value))
      .sort((left, right) => left.chat_id.localeCompare(right.chat_id)),
    messages: records
      .filter((record) => record.kind === 'message')
      .map((record) => clone(record.value))
      .sort((left, right) => left.message_id.localeCompare(right.message_id)),
  };
}

async function readState(transaction, meta) {
  const [outbox, chats, messages, snapshotRecords] = await Promise.all([
    transaction.getAll(INGESTION_STORES.outbox),
    transaction.getAll(INGESTION_STORES.chats),
    transaction.getAll(INGESTION_STORES.messages),
    transaction.getAll(INGESTION_STORES.snapshot),
  ]);
  return normalizeState({
    ...meta,
    outbox,
    chats,
    messages,
    pending_snapshot: materializedSnapshot(meta.pending_snapshot, snapshotRecords),
  });
}

async function replaceState(transaction, state) {
  await Promise.all(ALL_STORES.map((storeName) => transaction.clear(storeName)));
  await transaction.put(INGESTION_STORES.meta, metaFromState(state), INGESTION_META_KEY);
  await Promise.all(state.outbox.map((item) => transaction.put(INGESTION_STORES.outbox, item)));
  await Promise.all(state.chats.map((chat) => transaction.put(INGESTION_STORES.chats, chat)));
  await Promise.all(
    state.messages.map((message) => transaction.put(INGESTION_STORES.messages, message)),
  );
  if (state.pending_snapshot !== null) {
    await Promise.all([
      ...state.pending_snapshot.chats.map((chat) => transaction.put(
        INGESTION_STORES.snapshot,
        snapshotRecord('chat', chat.chat_id, chat),
      )),
      ...state.pending_snapshot.messages.map((message) => transaction.put(
        INGESTION_STORES.snapshot,
        snapshotRecord('message', message.message_id, message),
      )),
    ]);
  }
}

async function applyRawChange(transaction, change) {
  switch (change.type) {
    case 'chat.upsert':
      await transaction.put(INGESTION_STORES.chats, clone(change.chat));
      return [];
    case 'chat.delete': {
      await transaction.delete(INGESTION_STORES.chats, change.chat_id);
      const messageIds = await transaction.getAllKeysFromIndex(
        INGESTION_STORES.messages,
        'chat_id',
        change.chat_id,
      );
      await Promise.all(
        messageIds.map((messageId) => transaction.delete(INGESTION_STORES.messages, messageId)),
      );
      return messageIds;
    }
    case 'message.upsert':
      await transaction.put(INGESTION_STORES.messages, clone(change.message));
      return [];
    case 'message.delete':
      await transaction.delete(INGESTION_STORES.messages, change.message_id);
      return [];
    default:
      throw new Error(`Unsupported raw change type ${String(change.type)}`);
  }
}

function hydrateCache(state) {
  return {
    meta: metaFromState(state),
    outbox: new Map(state.outbox.map((item) => [item.source_seq, clone(item)])),
    chats: new Map(state.chats.map((chat) => [chat.chat_id, clone(chat)])),
    messages: new Map(state.messages.map((message) => [message.message_id, clone(message)])),
    pendingSnapshot: clone(state.pending_snapshot),
  };
}

/**
 * Durable ingestion storage contract.
 *
 * `storage.runTransaction(mode, storeNames, work)` must invoke `work` with a transaction-scoped
 * handle exposing `get`, `getAll`, `getAllKeys`, `getAllKeysFromIndex`, `put`, `delete`, and
 * `clear`. Every operation must be limited to `storeNames`; writes are committed atomically only
 * after `work` succeeds and the underlying transaction completes. A rejected operation or callback
 * aborts the whole write set. Callers must await every transaction operation and must not retain the
 * handle after `work` returns.
 */
export class DurableIngestOutbox {
  constructor({ storage, legacyStorage = null, idFactory = () => crypto.randomUUID() }) {
    if (typeof storage?.runTransaction !== 'function') {
      throw new Error('A transactional durable ingestion storage adapter is required');
    }
    if (
      legacyStorage !== null &&
      (
        typeof legacyStorage.loadLegacyIngestionState !== 'function' ||
        typeof legacyStorage.deleteLegacyIngestionState !== 'function'
      )
    ) {
      throw new Error('The legacy ingestion storage seam is invalid');
    }
    this.storage = storage;
    this.legacyStorage = legacyStorage;
    this.idFactory = idFactory;
    this.cache = null;
    this.initializing = null;
    this.writeChain = Promise.resolve();
  }

  async initialize() {
    if (this.cache !== null) return this.snapshotState();
    if (this.initializing === null) {
      this.initializing = (async () => {
        const legacyState = this.legacyStorage === null
          ? null
          : await this.legacyStorage.loadLegacyIngestionState();
        const state = await this.storage.runTransaction('readwrite', ALL_STORES, async (tx) => {
          const storedMeta = await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY);
          if (storedMeta !== undefined) {
            const storedState = await readState(tx, storedMeta);
            await tx.put(INGESTION_STORES.meta, metaFromState(storedState), INGESTION_META_KEY);
            return storedState;
          }

          const initial = normalizeState(legacyState);
          await replaceState(tx, initial);
          return initial;
        });
        if (legacyState !== null && legacyState !== undefined) {
          await this.legacyStorage.deleteLegacyIngestionState();
        }
        this.cache = hydrateCache(state);
        return this.snapshotState();
      })();
    }
    return this.initializing;
  }

  snapshotState() {
    if (this.cache === null) throw new Error('Durable outbox is not initialized');
    return {
      version: this.cache.meta.version,
      last_source_seq: this.cache.meta.last_source_seq,
      acknowledged_source_seq: this.cache.meta.acknowledged_source_seq,
      outbox: [...this.cache.outbox.values()]
        .sort((left, right) => left.source_seq - right.source_seq)
        .map(clone),
      chats: [...this.cache.chats.values()].map(clone),
      messages: [...this.cache.messages.values()].map(clone),
      pending_snapshot: clone(this.cache.pendingSnapshot),
    };
  }

  async entries() {
    await this.initialize();
    await this.writeChain;
    return [...this.cache.outbox.values()]
      .sort((left, right) => left.source_seq - right.source_seq)
      .map(clone);
  }

  async enqueue(change, eventId = this.idFactory()) {
    return this.queueMutation(async () => {
      const committed = await this.storage.runTransaction(
        'readwrite',
        [
          INGESTION_STORES.meta,
          INGESTION_STORES.outbox,
          INGESTION_STORES.chats,
          INGESTION_STORES.messages,
        ],
        async (tx) => {
          const meta = await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY);
          const sourceSeq = meta.last_source_seq + 1;
          const item = { event_id: eventId, source_seq: sourceSeq, change: clone(change) };
          const nextMeta = { ...meta, last_source_seq: sourceSeq };
          await tx.put(INGESTION_STORES.meta, nextMeta, INGESTION_META_KEY);
          await tx.put(INGESTION_STORES.outbox, item);
          const deletedMessageIds = await applyRawChange(tx, item.change);
          return { item, nextMeta, deletedMessageIds };
        },
      );

      this.cache.meta = clone(committed.nextMeta);
      this.cache.outbox.set(committed.item.source_seq, clone(committed.item));
      this.applyChangeToCache(committed.item.change, committed.deletedMessageIds);
      return clone(committed.item);
    });
  }

  async createSnapshot(snapshotId = this.idFactory()) {
    return this.queueMutation(async () => {
      const committed = await this.storage.runTransaction(
        'readwrite',
        [
          INGESTION_STORES.meta,
          INGESTION_STORES.chats,
          INGESTION_STORES.messages,
          INGESTION_STORES.snapshot,
        ],
        async (tx) => {
          const meta = await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY);
          if (meta.pending_snapshot !== null) {
            const records = await tx.getAll(INGESTION_STORES.snapshot);
            return { snapshot: materializedSnapshot(meta.pending_snapshot, records), meta };
          }

          const [chats, messages] = await Promise.all([
            tx.getAll(INGESTION_STORES.chats),
            tx.getAll(INGESTION_STORES.messages),
          ]);
          chats.sort((left, right) => left.chat_id.localeCompare(right.chat_id));
          messages.sort((left, right) => left.message_id.localeCompare(right.message_id));
          const pendingMetadata = {
            snapshot_id: snapshotId,
            through_seq: meta.last_source_seq,
          };
          const nextMeta = { ...meta, pending_snapshot: pendingMetadata };
          await tx.clear(INGESTION_STORES.snapshot);
          await Promise.all([
            ...chats.map((chat) => tx.put(
              INGESTION_STORES.snapshot,
              snapshotRecord('chat', chat.chat_id, chat),
            )),
            ...messages.map((message) => tx.put(
              INGESTION_STORES.snapshot,
              snapshotRecord('message', message.message_id, message),
            )),
          ]);
          await tx.put(INGESTION_STORES.meta, nextMeta, INGESTION_META_KEY);
          return {
            snapshot: { ...pendingMetadata, chats: clone(chats), messages: clone(messages) },
            meta: nextMeta,
          };
        },
      );
      this.cache.meta = clone(committed.meta);
      this.cache.pendingSnapshot = clone(committed.snapshot);
      return clone(committed.snapshot);
    });
  }

  async acknowledge(committedSourceSeq, snapshotId = null) {
    return this.queueMutation(async () => {
      const committed = await this.storage.runTransaction(
        'readwrite',
        [INGESTION_STORES.meta, INGESTION_STORES.outbox, INGESTION_STORES.snapshot],
        async (tx) => {
          const meta = await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY);
          const committedSeq = Math.max(meta.acknowledged_source_seq, committedSourceSeq);
          if (!Number.isSafeInteger(committedSourceSeq) || committedSourceSeq > meta.last_source_seq) {
            throw new Error('Acknowledgment exceeds the locally recorded source sequence');
          }
          const outboxKeys = await tx.getAllKeys(INGESTION_STORES.outbox);
          const deletedOutboxKeys = outboxKeys.filter((sourceSeq) => sourceSeq <= committedSeq);
          await Promise.all(
            deletedOutboxKeys.map((sourceSeq) => tx.delete(INGESTION_STORES.outbox, sourceSeq)),
          );
          const snapshotAcknowledged =
            meta.pending_snapshot !== null &&
            snapshotId !== null &&
            meta.pending_snapshot.snapshot_id === snapshotId &&
            committedSeq >= meta.pending_snapshot.through_seq;
          if (snapshotAcknowledged) await tx.clear(INGESTION_STORES.snapshot);
          const nextMeta = {
            ...meta,
            acknowledged_source_seq: committedSeq,
            pending_snapshot: snapshotAcknowledged ? null : meta.pending_snapshot,
          };
          await tx.put(INGESTION_STORES.meta, nextMeta, INGESTION_META_KEY);
          return { snapshotAcknowledged, committedSourceSeq: committedSeq, nextMeta };
        },
      );

      this.cache.meta = clone(committed.nextMeta);
      for (const sourceSeq of this.cache.outbox.keys()) {
        if (sourceSeq <= committed.committedSourceSeq) this.cache.outbox.delete(sourceSeq);
      }
      if (committed.snapshotAcknowledged) this.cache.pendingSnapshot = null;
      return {
        snapshotAcknowledged: committed.snapshotAcknowledged,
        committedSourceSeq: committed.committedSourceSeq,
      };
    });
  }

  async queueMutation(operation) {
    await this.initialize();
    const write = this.writeChain.then(operation);
    this.writeChain = write.then(() => undefined, () => undefined);
    return write;
  }

  applyChangeToCache(change, deletedMessageIds) {
    switch (change.type) {
      case 'chat.upsert':
        this.cache.chats.set(change.chat.chat_id, clone(change.chat));
        return;
      case 'chat.delete':
        this.cache.chats.delete(change.chat_id);
        for (const messageId of deletedMessageIds) this.cache.messages.delete(messageId);
        return;
      case 'message.upsert':
        this.cache.messages.set(change.message.message_id, clone(change.message));
        return;
      case 'message.delete':
        this.cache.messages.delete(change.message_id);
        return;
      default:
        throw new Error(`Unsupported raw change type ${String(change.type)}`);
    }
  }
}
