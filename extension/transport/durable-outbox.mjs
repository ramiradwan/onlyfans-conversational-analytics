import {
  InvariantViolation,
  entityForChange,
  mergeChat,
  mergeMessage,
} from './entity-merge.mjs';

const clone = (value) => structuredClone(value);

export const INGESTION_STATE_VERSION = 2;
export const INGESTION_META_KEY = 'state';
export const APPLIED_CONFIG_KEY = 'active';
export const COMMAND_STATE_KEY = 'state';
export const SNAPSHOT_MAX_FRAME_BYTES = 524_288;
export const SNAPSHOT_TARGET_BYTES = 458_752;
export const SNAPSHOT_MAX_RECORD_BYTES = 393_216;
export const SNAPSHOT_MAX_RECORDS = 100;
export const COVERAGE_SOURCE_SEQUENCE_INDEX = 'last_source_seq';

export const INGESTION_STORES = Object.freeze({
  meta: 'meta',
  outbox: 'outbox',
  chats: 'chats',
  messages: 'messages',
  coverageEvidence: 'coverage_evidence',
  historyJobs: 'history_jobs',
  commandResults: 'command_results',
  config: 'config',
  snapshotManifests: 'snapshot_manifests',
  snapshotChunks: 'snapshot_chunks',
  snapshotOverrides: 'snapshot_overrides',
  credentials: 'credentials',
});

const ALL_STORES = Object.freeze(Object.values(INGESTION_STORES));
const SNAPSHOT_KINDS = Object.freeze(['chat', 'message', 'coverage_evidence']);
const STORE_FOR_KIND = Object.freeze({
  chat: INGESTION_STORES.chats,
  message: INGESTION_STORES.messages,
  coverage_evidence: INGESTION_STORES.coverageEvidence,
});

function encodedBytes(value) {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  }
  return value;
}

function exactlyEqual(left, right) {
  return JSON.stringify(stable(left)) === JSON.stringify(stable(right));
}

function assertNonEmptyString(value, label) {
  if (typeof value !== 'string' || value.length === 0) throw new Error(`${label} is required`);
  return value;
}

function emptyMeta(creatorAccountId, agentStreamId) {
  return {
    version: INGESTION_STATE_VERSION,
    creator_account_id: creatorAccountId,
    agent_stream_id: agentStreamId,
    account_epoch: 1,
    last_source_seq: 0,
    acknowledged_source_seq: 0,
    applied_config_revision: null,
    outbox_count: 0,
    entity_counts: { chats: 0, messages: 0, coverage_evidence: 0 },
    pending_snapshot: null,
  };
}

function validateMeta(meta, creatorAccountId) {
  if (
    meta?.version !== INGESTION_STATE_VERSION
    || meta.creator_account_id !== creatorAccountId
    || typeof meta.agent_stream_id !== 'string'
    || !Number.isSafeInteger(meta.account_epoch)
    || meta.account_epoch < 1
    || !Number.isSafeInteger(meta.last_source_seq)
    || meta.last_source_seq < 0
    || !Number.isSafeInteger(meta.acknowledged_source_seq)
    || meta.acknowledged_source_seq < 0
    || meta.acknowledged_source_seq > meta.last_source_seq
    || !Number.isSafeInteger(meta.outbox_count)
    || meta.outbox_count < 0
  ) {
    throw new Error('Stored account-partitioned Agent state is invalid');
  }
  return clone(meta);
}

function materialFromEnvelope(envelope) {
  if (envelope === undefined || envelope === null) return null;
  const material = clone(envelope);
  delete material.last_source_seq;
  delete material.last_origin;
  return material;
}

function evidenceKey(evidence) {
  const generation = assertNonEmptyString(evidence?.generation_id, 'Coverage generation_id');
  switch (evidence.type) {
    case 'generation.started': return `${generation}:00:started`;
    case 'inventory.member':
      return `${generation}:10:member:${assertNonEmptyString(evidence.conversation_id, 'conversation_id')}`;
    case 'inventory.ended': return `${generation}:20:inventory-ended`;
    case 'conversation.history_started':
      return `${generation}:30:history:${assertNonEmptyString(evidence.conversation_id, 'conversation_id')}`;
    case 'conversation.head_reconciled':
      return `${generation}:40:head:${assertNonEmptyString(evidence.conversation_id, 'conversation_id')}`;
    case 'generation.closed': return `${generation}:50:closed`;
    default: throw new InvariantViolation('invalid_evidence', 'Unknown coverage evidence type');
  }
}

function snapshotOverrideKey(kind, id) {
  return `${kind}:${id}`;
}

function snapshotChunkKey(snapshotId, chunkIndex) {
  return `${snapshotId}:${String(chunkIndex).padStart(8, '0')}`;
}

function snapshotRecord(kind, envelope) {
  const material = materialFromEnvelope(envelope);
  if (kind === 'coverage_evidence') return clone(material.evidence);
  if (kind === 'chat') {
    return material.tombstone === true
      ? { tombstone: true, chat_id: material.chat_id }
      : { tombstone: false, chat: material };
  }
  return material.tombstone === true
    ? { tombstone: true, message_id: material.message_id, chat_id: material.chat_id }
    : { tombstone: false, message: material };
}

async function preserveSnapshotOverride(tx, meta, kind, id, existing) {
  const pending = meta.pending_snapshot;
  if (
    pending === null
    || pending.state !== 'building'
    || existing === undefined
    || existing === null
    || existing.last_source_seq > pending.through_seq
  ) return;
  const key = snapshotOverrideKey(kind, id);
  const saved = await tx.get(INGESTION_STORES.snapshotOverrides, key);
  if (saved === undefined) {
    await tx.put(INGESTION_STORES.snapshotOverrides, {
      key,
      snapshot_id: pending.snapshot_id,
      kind,
      entity_id: id,
      envelope: clone(existing),
    });
  }
}

function countField(kind) {
  if (kind === 'chat') return 'chats';
  if (kind === 'message') return 'messages';
  return 'coverage_evidence';
}

async function applyCoverageEvidence(tx, meta, change, sourceSeq, origin) {
  const evidence = clone(change.evidence);
  const key = evidenceKey(evidence);
  const existing = await tx.get(INGESTION_STORES.coverageEvidence, key);
  if (existing !== undefined) {
    if (exactlyEqual(existing.evidence, evidence)) return { action: 'noop', key };
    throw new InvariantViolation(
      'evidence_conflict',
      `Coverage evidence ${key} was reused with conflicting material`,
    );
  }
  await tx.put(INGESTION_STORES.coverageEvidence, {
    evidence_key: key,
    evidence,
    last_source_seq: sourceSeq,
    last_origin: origin,
  });
  meta.entity_counts.coverage_evidence += 1;
  return { action: 'insert', key };
}

async function applyEntityChange(tx, meta, change, sourceSeq, origin) {
  const entity = entityForChange(change);
  if (entity.kind === 'coverage_evidence') {
    return applyCoverageEvidence(tx, meta, change, sourceSeq, origin);
  }
  const storeName = STORE_FOR_KIND[entity.kind];
  const existing = await tx.get(storeName, entity.id);
  const existingMaterial = materialFromEnvelope(existing);
  const result = entity.kind === 'chat'
    ? mergeChat(existingMaterial, entity.value)
    : mergeMessage(existingMaterial, entity.value);
  if (result.action === 'noop') return result;
  await preserveSnapshotOverride(tx, meta, entity.kind, entity.id, existing);
  await tx.put(storeName, {
    ...clone(result.value),
    last_source_seq: sourceSeq,
    last_origin: origin,
  });
  if (existing === undefined) meta.entity_counts[countField(entity.kind)] += 1;
  return result;
}

async function appendChange(tx, meta, change, origin, eventId) {
  const candidateSeq = meta.last_source_seq + 1;
  const merged = await applyEntityChange(tx, meta, change, candidateSeq, origin);
  if (merged.action === 'noop') return null;
  const item = {
    event_id: eventId,
    source_seq: candidateSeq,
    acquisition_origin: origin,
    change: clone(change),
  };
  await tx.put(INGESTION_STORES.outbox, item);
  meta.last_source_seq = candidateSeq;
  meta.outbox_count += 1;
  return item;
}

function cloneMetaForWrite(meta) {
  const result = clone(meta);
  result.entity_counts ??= { chats: 0, messages: 0, coverage_evidence: 0 };
  return result;
}

/** Account-partitioned durable truth/outbox and bounded snapshot builder. */
export class DurableIngestOutbox {
  constructor({
    storage,
    creatorAccountId,
    idFactory = () => crypto.randomUUID(),
  }) {
    if (typeof storage?.runTransaction !== 'function') {
      throw new Error('A transactional durable ingestion storage adapter is required');
    }
    this.creatorAccountId = assertNonEmptyString(creatorAccountId, 'creatorAccountId');
    this.storage = storage;
    this.idFactory = idFactory;
    this.meta = null;
    this.initializing = null;
    this.writeChain = Promise.resolve();
    this.invalidated = false;
  }

  async initialize() {
    if (this.meta !== null) return this.identityState();
    if (this.initializing === null) {
      this.initializing = this.storage.runTransaction(
        'readwrite',
        [INGESTION_STORES.meta],
        async (tx) => {
          let meta = await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY);
          if (meta === undefined) {
            meta = emptyMeta(this.creatorAccountId, this.idFactory());
            await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
          }
          this.meta = validateMeta(meta, this.creatorAccountId);
          return this.identityState();
        },
      );
    }
    return this.initializing;
  }

  identityState() {
    if (this.meta === null) throw new Error('Durable outbox is not initialized');
    return {
      creator_account_id: this.meta.creator_account_id,
      agent_stream_id: this.meta.agent_stream_id,
      account_epoch: this.meta.account_epoch,
      last_source_seq: this.meta.last_source_seq,
      acknowledged_source_seq: this.meta.acknowledged_source_seq,
      applied_config_revision: this.meta.applied_config_revision,
      outbox_count: this.meta.outbox_count,
      entity_counts: clone(this.meta.entity_counts),
      pending_snapshot: clone(this.meta.pending_snapshot),
    };
  }

  snapshotState() {
    return this.identityState();
  }

  async entriesPage(afterSourceSeq = null, limit = SNAPSHOT_MAX_RECORDS) {
    await this.initialize();
    return this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.outbox],
      async (tx) => (await tx.getPage(
        INGESTION_STORES.outbox,
        { afterKey: afterSourceSeq, limit },
      )).map(({ value }) => clone(value)),
    );
  }

  /** Compatibility/testing helper; production transport uses entriesPage. */
  async entries() {
    const values = [];
    let after = null;
    while (true) {
      const page = await this.entriesPage(after, SNAPSHOT_MAX_RECORDS);
      values.push(...page);
      if (page.length < SNAPSHOT_MAX_RECORDS) return values;
      after = page.at(-1).source_seq;
    }
  }

  async enqueue(change, eventId = this.idFactory(), origin = 'passive') {
    return this.queueMutation(async () => {
      const item = await this.storage.runTransaction(
        'readwrite',
        [
          INGESTION_STORES.meta,
          INGESTION_STORES.outbox,
          INGESTION_STORES.chats,
          INGESTION_STORES.messages,
          INGESTION_STORES.coverageEvidence,
          INGESTION_STORES.snapshotOverrides,
        ],
        async (tx) => {
          const meta = cloneMetaForWrite(await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY));
          const appended = await appendChange(tx, meta, change, origin, eventId);
          if (this.invalidated) throw new Error('Account partition was invalidated');
          if (appended !== null) await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
          return { appended, meta };
        },
      );
      this.meta = clone(item.meta);
      return clone(item.appended);
    });
  }

  async enqueueMessageWithParent(messageChange, parentChange, origin = 'passive') {
    if (messageChange?.type !== 'message.upsert' || parentChange?.type !== 'chat.upsert') {
      throw new Error('Dependency-closed capture requires chat.upsert then message.upsert');
    }
    if (messageChange.message?.chat_id !== parentChange.chat?.chat_id) {
      throw new Error('Message and placeholder parent must use the same chat_id');
    }
    return this.queueMutation(async () => {
      const committed = await this.storage.runTransaction(
        'readwrite',
        [
          INGESTION_STORES.meta,
          INGESTION_STORES.outbox,
          INGESTION_STORES.chats,
          INGESTION_STORES.messages,
          INGESTION_STORES.coverageEvidence,
          INGESTION_STORES.snapshotOverrides,
        ],
        async (tx) => {
          const meta = cloneMetaForWrite(await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY));
          const parent = await appendChange(tx, meta, parentChange, origin, this.idFactory());
          const message = await appendChange(tx, meta, messageChange, origin, this.idFactory());
          if (this.invalidated) throw new Error('Account partition was invalidated');
          if (parent !== null || message !== null) {
            await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
          }
          return { parent, message, meta };
        },
      );
      this.meta = clone(committed.meta);
      return {
        items: [committed.parent, committed.message].filter(Boolean).map(clone),
        messageItem: clone(committed.message),
        parentCreated: committed.parent !== null,
      };
    });
  }

  async saveHistoryJob(job, validateAuthorization = null) {
    assertNonEmptyString(job?.job_id, 'history job_id');
    return this.queueMutation(async () => this.storage.runTransaction(
      'readwrite',
      [INGESTION_STORES.meta, INGESTION_STORES.historyJobs],
      async (tx) => {
        if (validateAuthorization !== null) await validateAuthorization();
        const meta = await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY);
        if (job.account_epoch !== meta.account_epoch) throw new Error('History job account epoch is stale');
        if (validateAuthorization !== null) await validateAuthorization();
        await tx.put(INGESTION_STORES.historyJobs, clone(job));
        if (this.invalidated) throw new Error('Account partition was invalidated');
        return clone(job);
      },
    ));
  }

  async historyJobs(limit = 1_000) {
    await this.initialize();
    return this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.historyJobs],
      async (tx) => (await tx.getPage(
        INGESTION_STORES.historyJobs,
        { afterKey: null, limit },
      )).map(({ value }) => clone(value)),
    );
  }

  async historyJobsPage(afterJobId = null, limit = 500) {
    await this.initialize();
    if (afterJobId !== null && (typeof afterJobId !== 'string' || afterJobId.length === 0)) {
      throw new Error('History job page cursor must be a non-empty job_id or null');
    }
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 1_000) {
      throw new Error('History job page limit must be between 1 and 1000');
    }
    return this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.historyJobs],
      async (tx) => (await tx.getPage(
        INGESTION_STORES.historyJobs,
        { afterKey: afterJobId, limit },
      )).map(({ value }) => clone(value)),
    );
  }

  async historyJob(jobId) {
    await this.initialize();
    assertNonEmptyString(jobId, 'history job_id');
    return this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.historyJobs],
      async (tx) => clone(await tx.get(INGESTION_STORES.historyJobs, jobId) ?? null),
    );
  }

  async historyConversationJobsPage(
    generationId,
    { afterJobId = null, limit = 500 } = {},
  ) {
    await this.initialize();
    const generation = assertNonEmptyString(generationId, 'history generation_id');
    const prefix = `${generation}:conversation:`;
    if (
      afterJobId !== null
      && (typeof afterJobId !== 'string' || !afterJobId.startsWith(prefix))
    ) {
      throw new Error('Conversation job page cursor belongs to another generation');
    }
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 1_000) {
      throw new Error('Conversation job page limit must be between 1 and 1000');
    }
    return this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.historyJobs],
      async (tx) => {
        const rows = await tx.getPage(INGESTION_STORES.historyJobs, {
          afterKey: afterJobId ?? prefix,
          limit,
        });
        const jobs = [];
        let reachedEnd = rows.length < limit;
        for (const row of rows) {
          if (!String(row.key).startsWith(prefix)) {
            reachedEnd = true;
            break;
          }
          jobs.push(clone(row.value));
        }
        return {
          jobs,
          next_after_job_id: reachedEnd ? null : jobs.at(-1)?.job_id ?? null,
        };
      },
    );
  }

  async hasChatOutsideHistoryGeneration(generationId) {
    await this.initialize();
    const generation = assertNonEmptyString(generationId, 'history generation_id');
    return this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.chats, INGESTION_STORES.historyJobs],
      async (tx) => {
        let afterKey = null;
        while (true) {
          const page = await tx.getPage(INGESTION_STORES.chats, { afterKey, limit: 100 });
          for (const row of page) {
            if (row.value?.tombstone === true) continue;
            const member = await tx.get(
              INGESTION_STORES.historyJobs,
              `${generation}:conversation:${row.key}`,
            );
            if (
              member === undefined
              || member.kind !== 'conversation'
              || member.generation_id !== generation
            ) return true;
          }
          if (page.length < 100) return false;
          afterKey = page.at(-1).key;
        }
      },
    );
  }

  async chatIds() {
    await this.initialize();
    return this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.chats],
      async (tx) => {
        const ids = [];
        let afterKey = null;
        while (true) {
          const page = await tx.getPage(INGESTION_STORES.chats, { afterKey, limit: 100 });
          for (const row of page) {
            if (row.value?.tombstone !== true) ids.push(row.key);
          }
          if (page.length < 100) break;
          afterKey = page.at(-1).key;
        }
        return ids;
      },
    );
  }

  async coverageEvidenceForGeneration(generationId) {
    await this.initialize();
    const prefix = `${generationId}:`;
    const values = [];
    let after = null;
    while (true) {
      const page = await this.storage.runTransaction(
        'readonly',
        [INGESTION_STORES.coverageEvidence],
        (tx) => tx.getPage(
          INGESTION_STORES.coverageEvidence,
          { afterKey: after, limit: 100 },
        ),
      );
      for (const row of page) {
        if (String(row.key).startsWith(prefix)) values.push(clone(row.value.evidence));
      }
      if (page.length < 100) return values;
      after = page.at(-1).key;
    }
  }

  /**
   * Atomically commits one normalized signer page. The upstream cursor advances
   * only after every material entity, evidence event, sequence, and outbox row commits.
   */
  async commitPage({
    jobId,
    expectedAccountEpoch,
    expectedLeaseToken,
    changes = [],
    evidence = [],
    nextCursor = null,
    boundary = null,
    jobPatch = {},
    spawnJobs = [],
    validateAuthorization = null,
  }) {
    return this.queueMutation(async () => {
      const result = await this.storage.runTransaction(
        'readwrite',
        [
          INGESTION_STORES.meta,
          INGESTION_STORES.outbox,
          INGESTION_STORES.chats,
          INGESTION_STORES.messages,
          INGESTION_STORES.coverageEvidence,
          INGESTION_STORES.historyJobs,
          INGESTION_STORES.snapshotOverrides,
        ],
        async (tx) => {
          if (validateAuthorization !== null) await validateAuthorization();
          const meta = cloneMetaForWrite(await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY));
          if (meta.account_epoch !== expectedAccountEpoch) throw new Error('Account epoch changed');
          const job = await tx.get(INGESTION_STORES.historyJobs, jobId);
          if (job === undefined || job.lease_token !== expectedLeaseToken) {
            throw new Error('History job lease is stale');
          }
          const appended = [];
          for (const change of changes) {
            const item = await appendChange(tx, meta, change, 'signer', this.idFactory());
            if (item !== null) appended.push(item);
          }
          for (const itemEvidence of evidence) {
            const item = await appendChange(
              tx,
              meta,
              { type: 'coverage.observed', evidence: itemEvidence },
              'signer',
              this.idFactory(),
            );
            if (item !== null) appended.push(item);
          }
          if (this.invalidated) throw new Error('Account partition was invalidated');
          const nextJob = {
            ...clone(job),
            ...clone(jobPatch),
            cursor: nextCursor,
            boundary,
            committed_pages: (job.committed_pages ?? 0) + 1,
            updated_at: new Date().toISOString(),
          };
          await tx.put(INGESTION_STORES.historyJobs, nextJob);
          for (const spawned of spawnJobs) {
            if (spawned.account_epoch !== meta.account_epoch) {
              throw new Error('Spawned history job account epoch is stale');
            }
            const existingSpawn = await tx.get(INGESTION_STORES.historyJobs, spawned.job_id);
            if (existingSpawn === undefined) {
              await tx.put(INGESTION_STORES.historyJobs, clone(spawned));
            } else if (!exactlyEqual(existingSpawn, spawned)) {
              throw new InvariantViolation(
                'history_job_conflict',
                `History job ${spawned.job_id} was reused with conflicting state`,
              );
            }
          }
          if (validateAuthorization !== null) await validateAuthorization();
          if (meta.account_epoch !== expectedAccountEpoch) throw new Error('Account epoch changed');
          await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
          if (this.invalidated) throw new Error('Account partition was invalidated');
          if (validateAuthorization !== null) await validateAuthorization();
          return { meta, job: nextJob, appended };
        },
      );
      this.meta = clone(result.meta);
      return { job: clone(result.job), items: result.appended.map(clone) };
    });
  }

  async createSnapshot(snapshotId = this.idFactory()) {
    await this.initialize();
    return this.queueMutation(async () => {
      const result = await this.storage.runTransaction(
        'readwrite',
        [
          INGESTION_STORES.meta,
          INGESTION_STORES.snapshotManifests,
          INGESTION_STORES.snapshotChunks,
          INGESTION_STORES.snapshotOverrides,
        ],
        async (tx) => {
          const meta = cloneMetaForWrite(await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY));
          if (meta.pending_snapshot !== null) {
            const existing = await tx.get(
              INGESTION_STORES.snapshotManifests,
              meta.pending_snapshot.snapshot_id,
            );
            if (existing === undefined) throw new Error('Pending snapshot manifest is missing');
            return { manifest: existing, meta };
          }
          await tx.clear(INGESTION_STORES.snapshotChunks);
          await tx.clear(INGESTION_STORES.snapshotOverrides);
          const manifest = {
            snapshot_id: snapshotId,
            through_seq: meta.last_source_seq,
            state: 'building',
            scan_kind_index: 0,
            scan_after_key: null,
            next_chunk_index: 0,
            chunk_count: null,
            record_counts: { chats: 0, messages: 0, coverage_evidence: 0 },
          };
          meta.pending_snapshot = {
            snapshot_id: snapshotId,
            through_seq: manifest.through_seq,
            state: 'building',
            next_expected_chunk_index: 0,
          };
          await tx.put(INGESTION_STORES.snapshotManifests, manifest);
          await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
          if (this.invalidated) throw new Error('Account partition was invalidated');
          return { manifest, meta };
        },
      );
      this.meta = clone(result.meta);
      return clone(result.manifest);
    });
  }

  async buildNextSnapshotChunk() {
    return this.queueMutation(async () => {
      const result = await this.storage.runTransaction(
        'readwrite',
        [
          INGESTION_STORES.meta,
          INGESTION_STORES.chats,
          INGESTION_STORES.messages,
          INGESTION_STORES.coverageEvidence,
          INGESTION_STORES.snapshotManifests,
          INGESTION_STORES.snapshotChunks,
          INGESTION_STORES.snapshotOverrides,
        ],
        async (tx) => {
          const meta = cloneMetaForWrite(await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY));
          if (meta.pending_snapshot === null) throw new Error('No pending snapshot');
          const manifest = await tx.get(
            INGESTION_STORES.snapshotManifests,
            meta.pending_snapshot.snapshot_id,
          );
          if (manifest === undefined) throw new Error('Pending snapshot manifest is missing');
          if (manifest.state === 'ready') return { manifest, chunk: null, meta };

          const kind = SNAPSHOT_KINDS[manifest.scan_kind_index];
          const storeName = STORE_FOR_KIND[kind];
          const page = kind === 'coverage_evidence'
            ? await tx.getPageFromIndex(
                storeName,
                COVERAGE_SOURCE_SEQUENCE_INDEX,
                {
                  afterIndexKey: manifest.scan_after_key,
                  limit: SNAPSHOT_MAX_RECORDS,
                },
              )
            : await tx.getPage(storeName, {
                afterKey: manifest.scan_after_key,
                limit: SNAPSHOT_MAX_RECORDS,
              });
          const records = [];
          let consumedKey = manifest.scan_after_key;
          for (const row of page) {
            const override = await tx.get(
              INGESTION_STORES.snapshotOverrides,
              snapshotOverrideKey(kind, row.key),
            );
            const scanKey = kind === 'coverage_evidence' ? row.indexKey : row.key;
            const envelope = override?.snapshot_id === manifest.snapshot_id
              ? override.envelope
              : row.value.last_source_seq <= manifest.through_seq
                ? row.value
                : null;
            if (envelope === null) {
              consumedKey = scanKey;
              continue;
            }
            const record = snapshotRecord(kind, envelope);
            if (encodedBytes(record) > SNAPSHOT_MAX_RECORD_BYTES) {
              throw new InvariantViolation(
                'snapshot_record_oversize',
                `${kind} ${String(row.key)} exceeds 384 KiB`,
              );
            }
            const candidate = [...records, record];
            if (records.length > 0 && encodedBytes({ records: candidate }) > SNAPSHOT_TARGET_BYTES) {
              break;
            }
            records.push(record);
            consumedKey = scanKey;
          }

          let chunk = null;
          if (records.length > 0) {
            chunk = {
              key: snapshotChunkKey(manifest.snapshot_id, manifest.next_chunk_index),
              snapshot_id: manifest.snapshot_id,
              chunk_index: manifest.next_chunk_index,
              entity_kind: kind,
              records,
            };
            await tx.put(INGESTION_STORES.snapshotChunks, chunk);
            manifest.next_chunk_index += 1;
            manifest.record_counts[countField(kind)] += records.length;
          }
          manifest.scan_after_key = consumedKey;
          const consumedWholePage = page.length === 0
            || consumedKey === (
              kind === 'coverage_evidence' ? page.at(-1)?.indexKey : page.at(-1)?.key
            );
          if (consumedWholePage && page.length < SNAPSHOT_MAX_RECORDS) {
            manifest.scan_kind_index += 1;
            manifest.scan_after_key = null;
          }
          if (manifest.scan_kind_index >= SNAPSHOT_KINDS.length) {
            manifest.state = 'ready';
            manifest.chunk_count = manifest.next_chunk_index;
            meta.pending_snapshot.state = 'ready';
            await tx.clear(INGESTION_STORES.snapshotOverrides);
          }
          if (this.invalidated) throw new Error('Account partition was invalidated');
          await tx.put(INGESTION_STORES.snapshotManifests, manifest);
          await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
          return { manifest, chunk, meta };
        },
      );
      this.meta = clone(result.meta);
      return { manifest: clone(result.manifest), chunk: clone(result.chunk) };
    });
  }

  async prepareSnapshot(snapshotId = this.idFactory()) {
    let manifest = await this.createSnapshot(snapshotId);
    while (manifest.state !== 'ready') {
      ({ manifest } = await this.buildNextSnapshotChunk());
    }
    return manifest;
  }

  async snapshotBeginFrame() {
    await this.initialize();
    const manifest = await this.currentSnapshotManifest();
    if (manifest?.state !== 'ready') throw new Error('Snapshot chunks are not finalized');
    return {
      frame_kind: 'begin',
      snapshot_id: manifest.snapshot_id,
      through_seq: manifest.through_seq,
      chunk_count: manifest.chunk_count,
      record_counts: clone(manifest.record_counts),
      max_frame_bytes: SNAPSHOT_MAX_FRAME_BYTES,
    };
  }

  async snapshotChunkFrame(chunkIndex) {
    await this.initialize();
    const manifest = await this.currentSnapshotManifest();
    if (manifest?.state !== 'ready') throw new Error('Snapshot chunks are not finalized');
    const chunk = await this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.snapshotChunks],
      (tx) => tx.get(
        INGESTION_STORES.snapshotChunks,
        snapshotChunkKey(manifest.snapshot_id, chunkIndex),
      ),
    );
    if (chunk === undefined) throw new Error(`Snapshot chunk ${chunkIndex} is missing`);
    return {
      frame_kind: 'chunk',
      snapshot_id: manifest.snapshot_id,
      chunk_index: chunk.chunk_index,
      entity_kind: chunk.entity_kind,
      records: clone(chunk.records),
    };
  }

  async snapshotCommitFrame() {
    const manifest = await this.currentSnapshotManifest();
    if (manifest?.state !== 'ready') throw new Error('Snapshot chunks are not finalized');
    return {
      frame_kind: 'commit',
      snapshot_id: manifest.snapshot_id,
      chunk_count: manifest.chunk_count,
    };
  }

  async currentSnapshotManifest() {
    await this.initialize();
    if (this.meta.pending_snapshot === null) return null;
    return this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.snapshotManifests],
      async (tx) => clone(await tx.get(
        INGESTION_STORES.snapshotManifests,
        this.meta.pending_snapshot.snapshot_id,
      )),
    );
  }

  async acknowledge(committedSourceSeq, snapshotId = null, snapshotProgress = null) {
    return this.queueMutation(async () => {
      const result = await this.storage.runTransaction(
        'readwrite',
        [
          INGESTION_STORES.meta,
          INGESTION_STORES.outbox,
          INGESTION_STORES.snapshotManifests,
          INGESTION_STORES.snapshotChunks,
          INGESTION_STORES.snapshotOverrides,
        ],
        async (tx) => {
          const meta = cloneMetaForWrite(await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY));
          if (!Number.isSafeInteger(committedSourceSeq) || committedSourceSeq > meta.last_source_seq) {
            throw new Error('Acknowledgment exceeds the locally recorded source sequence');
          }
          let snapshotAcknowledged = false;
          if (snapshotProgress !== null) {
            if (
              snapshotId !== snapshotProgress.snapshot_id
              || meta.pending_snapshot?.snapshot_id !== snapshotProgress.snapshot_id
            ) throw new Error('Snapshot acknowledgement identity does not match');
            meta.pending_snapshot.next_expected_chunk_index =
              snapshotProgress.next_expected_chunk_index;
            if (snapshotProgress.committed) {
              const manifest = await tx.get(
                INGESTION_STORES.snapshotManifests,
                snapshotProgress.snapshot_id,
              );
              if (manifest?.state !== 'ready' || committedSourceSeq < manifest.through_seq) {
                throw new Error('Snapshot commit acknowledgement does not cover the manifest');
              }
              for (let index = 0; index < manifest.chunk_count; index += 1) {
                await tx.delete(
                  INGESTION_STORES.snapshotChunks,
                  snapshotChunkKey(manifest.snapshot_id, index),
                );
              }
              await tx.delete(INGESTION_STORES.snapshotManifests, manifest.snapshot_id);
              await tx.clear(INGESTION_STORES.snapshotOverrides);
              meta.pending_snapshot = null;
              snapshotAcknowledged = true;
            }
          }

          const newCheckpoint = Math.max(meta.acknowledged_source_seq, committedSourceSeq);
          if (snapshotProgress === null || snapshotProgress.committed) {
            let after = null;
            while (true) {
              const page = await tx.getPage(INGESTION_STORES.outbox, { afterKey: after, limit: 100 });
              const removable = page.filter(({ key }) => key <= newCheckpoint);
              for (const row of removable) await tx.delete(INGESTION_STORES.outbox, row.key);
              meta.outbox_count -= removable.length;
              if (page.length < 100 || page.at(-1).key > newCheckpoint) break;
              after = page.at(-1).key;
            }
            meta.acknowledged_source_seq = newCheckpoint;
          }
          await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
          if (this.invalidated) throw new Error('Account partition was invalidated');
          return { meta, snapshotAcknowledged, committedSourceSeq: meta.acknowledged_source_seq };
        },
      );
      this.meta = clone(result.meta);
      return {
        snapshotAcknowledged: result.snapshotAcknowledged,
        committedSourceSeq: result.committedSourceSeq,
      };
    });
  }

  async loadAppliedConfig() {
    await this.initialize();
    return this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.config],
      async (tx) => clone((await tx.get(INGESTION_STORES.config, APPLIED_CONFIG_KEY))?.document ?? null),
    );
  }

  async saveAppliedConfig(document) {
    return this.queueMutation(async () => {
      const result = await this.storage.runTransaction(
        'readwrite',
        [INGESTION_STORES.meta, INGESTION_STORES.config],
        async (tx) => {
          const meta = cloneMetaForWrite(await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY));
          await tx.put(INGESTION_STORES.config, {
            key: APPLIED_CONFIG_KEY,
            document: clone(document),
          });
          meta.applied_config_revision = document.config_revision;
          await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
          if (this.invalidated) throw new Error('Account partition was invalidated');
          return meta;
        },
      );
      this.meta = clone(result);
    });
  }

  async clearAppliedConfig() {
    return this.queueMutation(async () => {
      const result = await this.storage.runTransaction(
        'readwrite',
        [INGESTION_STORES.meta, INGESTION_STORES.config],
        async (tx) => {
          const meta = cloneMetaForWrite(await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY));
          await tx.delete(INGESTION_STORES.config, APPLIED_CONFIG_KEY);
          meta.applied_config_revision = null;
          await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
          if (this.invalidated) throw new Error('Account partition was invalidated');
          return meta;
        },
      );
      this.meta = clone(result);
    });
  }

  async loadCommandState() {
    await this.initialize();
    return this.storage.runTransaction(
      'readonly',
      [INGESTION_STORES.commandResults],
      async (tx) => clone((await tx.get(
        INGESTION_STORES.commandResults,
        COMMAND_STATE_KEY,
      ))?.state ?? null),
    );
  }

  async saveCommandState(state) {
    return this.queueMutation(() => this.storage.runTransaction(
      'readwrite',
      [INGESTION_STORES.commandResults],
      async (tx) => {
        await tx.put(INGESTION_STORES.commandResults, {
          key: COMMAND_STATE_KEY,
          state: clone(state),
        });
        if (this.invalidated) throw new Error('Account partition was invalidated');
      },
    ));
  }

  invalidateAccountEpoch() {
    this.invalidated = true;
    const invalidate = this.writeChain.then(() => this.storage.runTransaction(
      'readwrite',
      [INGESTION_STORES.meta],
      async (tx) => {
        const meta = cloneMetaForWrite(await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY));
        meta.account_epoch += 1;
        await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
        this.meta = clone(meta);
      },
    ));
    this.writeChain = invalidate.then(() => undefined, () => undefined);
    return invalidate;
  }

  async queueMutation(operation) {
    await this.initialize();
    if (this.invalidated) throw new Error('Account partition was invalidated');
    const write = this.writeChain.then(() => {
      if (this.invalidated) throw new Error('Account partition was invalidated');
      return operation();
    });
    this.writeChain = write.then(() => undefined, () => undefined);
    return write;
  }
}
