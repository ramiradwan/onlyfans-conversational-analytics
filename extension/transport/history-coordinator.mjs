import {
  normalizeSignerConversation,
  normalizeSignerMessage,
} from './signer-normalization.mjs';

const TERMINAL_CONVERSATION_PHASES = new Set(['complete', 'failed']);
const MAX_RETRY_DELAY_MS = 3_600_000;
const HISTORY_JOB_PAGE_SIZE = 500;

function abortError(reason = 'History acquisition was cancelled') {
  const error = reason instanceof Error ? reason : new Error(reason);
  error.name = 'AbortError';
  return error;
}

function throwIfAborted(signal) {
  if (signal?.aborted) throw abortError(signal.reason);
}

function isAbort(error, signal) {
  return signal?.aborted === true || error?.name === 'AbortError';
}

function awaitWithAbort(value, signal) {
  throwIfAborted(signal);
  let listener;
  const aborted = new Promise((_, reject) => {
    listener = () => reject(abortError(signal.reason));
    signal.addEventListener('abort', listener, { once: true });
  });
  return Promise.race([Promise.resolve(value), aborted])
    .finally(() => signal.removeEventListener('abort', listener));
}

class RateLimitedError extends Error {
  constructor(retryAfterMs) {
    super('Signer history read was rate limited');
    this.code = 'rate_limited';
    this.retryAfterMs = Number.isSafeInteger(retryAfterMs) ? retryAfterMs : null;
  }
}

function assertPage(result, operation) {
  if (result?.response?.status === 429) {
    throw new RateLimitedError(result.response.retry_after_ms);
  }
  if (
    result?.success !== true
    || result.operation !== operation
    || !result.data
    || !Array.isArray(result.data.items)
    || (result.data.continuation !== null && typeof result.data.continuation !== 'string')
    || ![null, 'inventory_end', 'history_start'].includes(result.data.boundary)
    || Object.keys(result.data).length !== 3
    || Object.keys(result.data).some((key) => !['items', 'continuation', 'boundary'].includes(key))
  ) {
    throw new Error(`Signer ${operation} result is not a validated one-page result`);
  }
  if (result.data.boundary !== null && result.data.continuation !== null) {
    throw new Error(`Signer ${operation} returned a boundary with a continuation`);
  }
  return result.data;
}

function authorizationIdentity({ document, session, policy }) {
  return JSON.stringify({
    creator_account_id: document.creator_account_id,
    config_revision: document.config_revision,
    consent_revision: policy.consent_revision,
    authorized_platform_creator_id: policy.authorized_platform_creator_id,
    session_creator_account_id: session.creator_account_id,
    session_connection_id: session.connection_id ?? null,
    session_fencing_token: session.fencing_token ?? null,
    session_agent_stream_id: session.agent_stream_id ?? null,
    applied_config_revision: session.applied_config_revision,
  });
}

function newerInventory(current, candidate) {
  if (current === undefined) return candidate;
  const byAsOf = String(candidate.as_of ?? '').localeCompare(String(current.as_of ?? ''));
  if (byAsOf !== 0) return byAsOf > 0 ? candidate : current;
  return String(candidate.job_id).localeCompare(String(current.job_id)) > 0
    ? candidate
    : current;
}

function recentFirst(left, right) {
  if (Boolean(left.recent_priority) !== Boolean(right.recent_priority)) {
    return left.recent_priority ? -1 : 1;
  }
  const activity = String(right.last_activity_at ?? '').localeCompare(
    String(left.last_activity_at ?? ''),
  );
  if (activity !== 0) return activity;
  return String(left.conversation_id).localeCompare(String(right.conversation_id));
}

function earlier(left, right) {
  if (left === null) return right;
  if (right === null) return left;
  return Date.parse(left) <= Date.parse(right) ? left : right;
}

export class HistoryAcquisitionCoordinator {
  constructor({
    outbox,
    signer,
    configuration,
    session,
    idFactory = () => crypto.randomUUID(),
    now = () => new Date().toISOString(),
    delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
    clock = () => Date.now(),
  }) {
    if (
      !outbox?.commitPage
      || !outbox?.saveHistoryJob
      || !outbox?.historyJob
      || !outbox?.historyJobsPage
      || !outbox?.historyConversationJobsPage
      || !outbox?.hasChatOutsideHistoryGeneration
    ) {
      throw new Error('History coordinator requires the account-partitioned durable outbox');
    }
    if (!signer?.read) throw new Error('History coordinator requires a one-page signer');
    this.outbox = outbox;
    this.signer = signer;
    this.configuration = configuration;
    this.session = session;
    this.idFactory = idFactory;
    this.now = now;
    this.delay = delay;
    this.clock = clock;
    this.leaseToken = idFactory();
    this.running = null;
    this.runController = null;
    this.runAuthorizationIdentity = null;
    this.stopped = false;
    // Bounded backoff so a persistent signer/refresh failure cannot re-run on
    // every wake and reload the platform tab in a tight loop.
    this.backoffUntil = 0;
    this.failureStreak = 0;
  }

  wake() {
    if (this.stopped) return Promise.resolve({ status: 'stopped', pages: 0 });
    if (this.running === null && this.clock() < this.backoffUntil) {
      return Promise.resolve({ status: 'deferred', pages: 0 });
    }
    const authorization = this.#authorization();
    const nextIdentity = authorization === null ? null : authorizationIdentity(authorization);
    if (this.running !== null) {
      if (nextIdentity !== this.runAuthorizationIdentity) {
        const staleRun = this.running;
        this.cancelCurrent('History acquisition authorization changed');
        return staleRun
          .catch((error) => {
            if (!isAbort(error, this.runController?.signal)) throw error;
          })
          .then(() => this.wake());
      }
      return this.running;
    }
    const controller = new AbortController();
    this.runController = controller;
    this.runAuthorizationIdentity = nextIdentity;
    const run = this.#run(authorization, controller.signal).then(
      (result) => { this.failureStreak = 0; this.backoffUntil = 0; return result; },
      (error) => {
        if (!isAbort(error, controller.signal)) {
          this.failureStreak += 1;
          this.backoffUntil = this.clock() + Math.min(60_000, 3_000 * 2 ** (this.failureStreak - 1));
        }
        throw error;
      },
    );
    this.running = run.finally(() => {
      if (this.running === run || this.runController === controller) {
        this.running = null;
        this.runController = null;
        this.runAuthorizationIdentity = null;
      }
    });
    return this.running;
  }

  cancelCurrent(reason = 'History acquisition was cancelled') {
    if (this.runController !== null && !this.runController.signal.aborted) {
      this.runController.abort(abortError(reason));
    }
    return this.running;
  }

  stop() {
    this.stopped = true;
    this.cancelCurrent('History acquisition coordinator stopped');
  }

  #authorization() {
    const document = this.configuration();
    const boundSession = this.session();
    const policy = document?.history_acquisition;
    if (
      boundSession === null
      || boundSession === undefined
      || document?.creator_account_id !== boundSession.creator_account_id
      || document?.config_revision !== boundSession.applied_config_revision
      || policy?.enabled !== true
      || typeof policy.consent_revision !== 'string'
      || typeof policy.authorized_platform_creator_id !== 'string'
    ) return null;
    return { document, session: boundSession, policy };
  }

  async #run(authorization, signal) {
    throwIfAborted(signal);
    if (authorization === null) return { status: 'disabled', pages: 0 };
    const inventories = await this.#latestInventories(authorization, signal);
    let openInventory = inventories.open;
    if (openInventory === undefined) {
      const closed = inventories.closed;
      if (
        closed === undefined
        || await this.#requiresNewGeneration(closed, authorization, signal)
      ) {
        openInventory = await this.#startGeneration(authorization, signal);
      } else {
        return { status: 'current', pages: 0 };
      }
    }

    const budget = authorization.policy.pages_per_wake;
    let pages = 0;
    while (pages < budget) {
      this.#assertActive(signal, authorization);
      const inventory = await this.outbox.historyJob(openInventory.job_id);
      this.#assertActive(signal, authorization);
      if (
        inventory === null
        || inventory.kind !== 'inventory'
        || inventory.generation_id !== openInventory.generation_id
      ) throw new Error('Open history inventory job is missing or invalid');
      if (inventory.phase === 'start') {
        await this.#claimAndCommit(inventory, {
          evidence: [{
            type: 'generation.started',
            generation_id: inventory.generation_id,
            as_of: inventory.as_of,
            authorization_revision: authorization.policy.consent_revision,
          }],
          jobPatch: { phase: 'inventory' },
        }, authorization, signal);
        continue;
      }
      if (inventory.phase === 'inventory') {
        if (this.#deferred(inventory)) break;
        await this.#readInventoryPage(inventory, authorization, signal);
        pages += 1;
      } else {
        const conversations = await this.#conversationSummary(
          inventory.generation_id,
          authorization,
          signal,
        );
        if (conversations.next !== undefined) {
          await this.#readConversationPage(conversations.next, authorization, signal);
          pages += 1;
        } else if (conversations.hasPending) {
          break;
        } else {
          await this.#claimAndCommit(inventory, {
            evidence: [{
              type: 'generation.closed',
              generation_id: inventory.generation_id,
              closed_at: this.now(),
            }],
            jobPatch: {
              phase: 'closed',
              retry_count: inventory.retry_count ?? 0,
            },
          }, authorization, signal);
          break;
        }
      }
      if (pages < budget && authorization.policy.request_interval_ms > 0) {
        await awaitWithAbort(
          this.delay(authorization.policy.request_interval_ms, signal),
          signal,
        );
      }
    }
    return { status: 'progressed', pages };
  }

  #deferred(job) {
    if (typeof job.next_attempt_at !== 'string') return false;
    const nextAttempt = Date.parse(job.next_attempt_at);
    return Number.isFinite(nextAttempt) && nextAttempt > this.clock();
  }

  async #latestInventories(authorization, signal) {
    let afterJobId = null;
    let open;
    let closed;
    while (true) {
      const page = await this.outbox.historyJobsPage(afterJobId, HISTORY_JOB_PAGE_SIZE);
      this.#assertActive(signal, authorization);
      for (const job of page) {
        if (job.kind !== 'inventory') continue;
        if (job.phase === 'closed') closed = newerInventory(closed, job);
        else open = newerInventory(open, job);
      }
      if (page.length < HISTORY_JOB_PAGE_SIZE) return { open, closed };
      const nextAfter = page.at(-1)?.job_id;
      if (typeof nextAfter !== 'string' || nextAfter === afterJobId) {
        throw new Error('History job page cursor did not advance');
      }
      afterJobId = nextAfter;
    }
  }

  async #conversationSummary(generationId, authorization, signal) {
    let afterJobId = null;
    let hasPending = false;
    let hasFailed = false;
    let next;
    while (true) {
      const page = await this.outbox.historyConversationJobsPage(generationId, {
        afterJobId,
        limit: HISTORY_JOB_PAGE_SIZE,
      });
      this.#assertActive(signal, authorization);
      for (const job of page.jobs) {
        if (job.kind !== 'conversation' || job.generation_id !== generationId) {
          throw new Error('History conversation job is stored under the wrong generation');
        }
        if (job.phase === 'failed') hasFailed = true;
        if (TERMINAL_CONVERSATION_PHASES.has(job.phase)) continue;
        hasPending = true;
        if (!this.#deferred(job) && (next === undefined || recentFirst(job, next) < 0)) {
          next = job;
        }
      }
      if (page.next_after_job_id === null) return { hasPending, hasFailed, next };
      if (page.next_after_job_id === afterJobId) {
        throw new Error('Conversation job page cursor did not advance');
      }
      afterJobId = page.next_after_job_id;
    }
  }

  async #requiresNewGeneration(closedInventory, authorization, signal) {
    if (closedInventory.authorization_revision !== authorization.policy.consent_revision) return true;
    const conversations = await this.#conversationSummary(
      closedInventory.generation_id,
      authorization,
      signal,
    );
    if (conversations.hasFailed) return true;
    const hasUnknownChat = await this.outbox.hasChatOutsideHistoryGeneration(
      closedInventory.generation_id,
    );
    this.#assertActive(signal, authorization);
    return hasUnknownChat;
  }

  #assertAuthorization(expected, job = null) {
    const current = this.#authorization();
    if (current === null || authorizationIdentity(current) !== authorizationIdentity(expected)) {
      throw new Error('History acquisition authorization changed');
    }
    if (job !== null && this.outbox.identityState().account_epoch !== job.account_epoch) {
      throw new Error('History acquisition account epoch changed');
    }
    return current;
  }

  #assertActive(signal, expected, job = null) {
    throwIfAborted(signal);
    return this.#assertAuthorization(expected, job);
  }

  async #startGeneration({ policy, session }, signal) {
    const expected = this.#authorization();
    if (expected === null) throw new Error('History acquisition is not authorized');
    this.#assertActive(signal, expected);
    const identity = await this.signer.read({
      operation: 'identity',
      parameters: {},
      // The signer refreshes only when no generation exists or a validated read reports
      // stale authorization. Its Chrome host independently requires an inactive,
      // draft-free tab before reloading, so an ordinary healthy wake never reloads.
      refreshMode: 'allow',
      signal,
    });
    if (
      identity?.success !== true
      || identity.operation !== 'identity'
      || identity.data?.id !== policy.authorized_platform_creator_id
    ) throw new Error('Signer identity does not match the authorized platform creator');
    this.#assertActive(signal, expected);
    const generationId = this.idFactory();
    const state = this.outbox.identityState();
    const job = {
      job_id: `${generationId}:inventory`,
      generation_id: generationId,
      kind: 'inventory',
      phase: 'start',
      as_of: this.now(),
      cursor: null,
      boundary: null,
      committed_pages: 0,
      retry_count: 0,
      account_epoch: state.account_epoch,
      lease_token: this.leaseToken,
      creator_account_id: session.creator_account_id,
      authorization_revision: policy.consent_revision,
      recent_window_days: policy.recent_window_days,
    };
    await this.outbox.saveHistoryJob(
      job,
      () => this.#assertActive(signal, expected),
    );
    return job;
  }

  async #claim(job, authorization, signal) {
    this.#assertActive(signal, authorization, job);
    const claimed = { ...job, lease_token: this.leaseToken };
    await this.outbox.saveHistoryJob(
      claimed,
      () => this.#assertActive(signal, authorization, claimed),
    );
    return claimed;
  }

  async #claimAndCommit(job, page, authorization, signal) {
    this.#assertActive(signal, authorization, job);
    const claimed = job.lease_token === this.leaseToken
      ? job
      : await this.#claim(job, authorization, signal);
    this.#assertActive(signal, authorization, claimed);
    return this.outbox.commitPage({
      jobId: claimed.job_id,
      expectedAccountEpoch: claimed.account_epoch,
      expectedLeaseToken: this.leaseToken,
      changes: page.changes ?? [],
      evidence: page.evidence ?? [],
      nextCursor: page.nextCursor ?? claimed.cursor,
      boundary: page.boundary ?? claimed.boundary,
      jobPatch: { retry_count: 0, next_attempt_at: null, ...(page.jobPatch ?? {}) },
      spawnJobs: page.spawnJobs ?? [],
      validateAuthorization: () => this.#assertActive(signal, authorization, claimed),
    });
  }

  async #readInventoryPage(job, authorization, signal) {
    const { policy } = authorization;
    try {
      job = job.lease_token === this.leaseToken
        ? job
        : await this.#claim(job, authorization, signal);
      this.#assertActive(signal, authorization, job);
      const observedAt = this.now();
      const data = assertPage(await this.signer.read({
        operation: 'conversations',
        parameters: {
          query: { limit: policy.page_size, cursor: job.cursor },
        },
        refreshMode: 'allow',
        signal,
      }), 'conversations');
      this.#assertActive(signal, authorization, job);
      if (data.boundary !== null && data.boundary !== 'inventory_end') {
        throw new Error('Conversation inventory returned the wrong boundary');
      }
      if (data.continuation !== null && data.continuation === job.cursor) {
        throw new Error('Conversation inventory cursor repeated');
      }
      const changes = data.items.map((item) => normalizeSignerConversation(item, {
        observedAt,
        creatorPlatformId: policy.authorized_platform_creator_id,
      }));
      const conversationIds = changes.map((change) => change.chat.chat_id);
      const evidence = conversationIds.map((conversationId) => ({
        type: 'inventory.member',
        generation_id: job.generation_id,
        conversation_id: conversationId,
      }));
      const recentCutoff = Date.parse(job.as_of) - policy.recent_window_days * 86_400_000;
      const spawnJobs = changes.map((change) => ({
        conversation_id: change.chat.chat_id,
        last_activity_at: change.chat.updated_at,
        recent_priority: Date.parse(change.chat.updated_at) >= recentCutoff,
      })).map(({ conversation_id: conversationId, last_activity_at, recent_priority }) => ({
        job_id: `${job.generation_id}:conversation:${conversationId}`,
        generation_id: job.generation_id,
        kind: 'conversation',
        conversation_id: conversationId,
        phase: 'history',
        as_of: job.as_of,
        cursor: null,
        boundary: null,
        committed_pages: 0,
        retry_count: 0,
        earliest_observed_at: null,
        head_reconciled: false,
        account_epoch: job.account_epoch,
        lease_token: this.leaseToken,
        creator_account_id: job.creator_account_id,
        authorization_revision: job.authorization_revision,
        last_activity_at,
        recent_priority,
      }));
      const ended = data.boundary === 'inventory_end';
      if (ended) evidence.push({
        type: 'inventory.ended',
        generation_id: job.generation_id,
        observed_at: observedAt,
      });
      await this.#claimAndCommit(job, {
        changes,
        evidence,
        nextCursor: data.continuation,
        boundary: data.boundary,
        jobPatch: { phase: ended ? 'conversations' : 'inventory' },
        spawnJobs,
      }, authorization, signal);
    } catch (error) {
      if (isAbort(error, signal)) throw abortError(signal.reason);
      await this.#recordFailure(job, error, authorization, signal);
    }
  }

  async #readConversationPage(job, authorization, signal) {
    const { policy } = authorization;
    try {
      job = job.lease_token === this.leaseToken
        ? job
        : await this.#claim(job, authorization, signal);
      this.#assertActive(signal, authorization, job);
      const observedAt = this.now();
      const data = assertPage(await this.signer.read({
        operation: 'message-page',
        parameters: {
          conversationId: job.conversation_id,
          query: { limit: policy.page_size, cursor: job.cursor },
        },
        refreshMode: 'allow',
        signal,
      }), 'message-page');
      this.#assertActive(signal, authorization, job);
      if (data.boundary !== null && data.boundary !== 'history_start') {
        throw new Error('Message page returned the wrong boundary');
      }
      if (data.continuation !== null && data.continuation === job.cursor) {
        throw new Error('Message history cursor repeated');
      }
      const changes = data.items.map((item) => normalizeSignerMessage(item, {
        observedAt,
        creatorPlatformId: policy.authorized_platform_creator_id,
        conversationId: job.conversation_id,
      }));
      let earliestObservedAt = job.earliest_observed_at;
      for (const change of changes) earliestObservedAt = earlier(
        earliestObservedAt,
        change.message.sent_at,
      );
      const evidence = [];
      if (!job.head_reconciled) evidence.push({
        type: 'conversation.head_reconciled',
        generation_id: job.generation_id,
        conversation_id: job.conversation_id,
        reconciled_through: observedAt,
      });
      const completed = data.boundary === 'history_start';
      if (completed) evidence.push({
        type: 'conversation.history_started',
        generation_id: job.generation_id,
        conversation_id: job.conversation_id,
        earliest_observed_at: earliestObservedAt,
        observed_at: observedAt,
      });
      await this.#claimAndCommit(job, {
        changes,
        evidence,
        nextCursor: data.continuation,
        boundary: data.boundary,
        jobPatch: {
          phase: completed ? 'complete' : 'history',
          earliest_observed_at: earliestObservedAt,
          head_reconciled: true,
        },
      }, authorization, signal);
    } catch (error) {
      if (isAbort(error, signal)) throw abortError(signal.reason);
      await this.#recordFailure(job, error, authorization, signal);
    }
  }

  async #recordFailure(job, error, authorization, signal) {
    this.#assertActive(signal, authorization, job);
    const retries = (job.retry_count ?? 0) + 1;
    const claimed = job.lease_token === this.leaseToken
      ? job
      : await this.#claim(job, authorization, signal);
    this.#assertActive(signal, authorization, claimed);
    const rateLimited = error?.code === 'rate_limited';
    const exponentialDelay = Math.max(1_000, authorization.policy.request_interval_ms)
      * 2 ** Math.max(0, retries - 1);
    const retryDelay = rateLimited
      ? Math.min(
          MAX_RETRY_DELAY_MS,
          Math.max(error.retryAfterMs ?? 0, exponentialDelay),
        )
      : 0;
    await this.outbox.commitPage({
      jobId: claimed.job_id,
      expectedAccountEpoch: claimed.account_epoch,
      expectedLeaseToken: this.leaseToken,
      nextCursor: claimed.cursor,
      boundary: claimed.boundary,
      jobPatch: {
        retry_count: retries,
        phase: retries > authorization.policy.retry_limit ? 'failed' : claimed.phase,
        last_error_code: typeof error?.code === 'string' ? error.code : 'read_failed',
        next_attempt_at: rateLimited && retries <= authorization.policy.retry_limit
          ? new Date(this.clock() + retryDelay).toISOString()
          : null,
      },
      validateAuthorization: () => this.#assertActive(signal, authorization, claimed),
    });
    if (!rateLimited && retries <= authorization.policy.retry_limit) throw error;
  }
}
