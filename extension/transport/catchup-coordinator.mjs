import { MAX_CURSOR_LENGTH, SAFE_CURSOR } from 'local-authenticated-read-connector/browser-signing';
import { normalizeSignerConversation, normalizeSignerMessage } from './read-only-signer-normalization.mjs';
import { AcquisitionAllowance, emptyCounters } from './acquisition-allowance.mjs';
import { parseCaptureStateReportResponse, parseHistoryCheckBeginResponse } from '../protocol/read-only.mjs';

const ACTIVE = 'catchup:active';
const terminal = job => !job || ['completed', 'abandoned'].includes(job.phase);
const blocked = new Set(['capture_off', 'consent_needed', 'paused', 'account_mismatch',
  'storage_locked', 'no_onlyfans_tab', 'tab_frozen', 'tab_discarded']);
const failure = code => Object.assign(new Error('Check cannot continue'), { code });
const copy = value => structuredClone(value);
const head = value => ({ ...(value.head_message_id ? { message_id: value.head_message_id } : {}),
  ...(value.head_sent_at ? { sent_at: value.head_sent_at } : {}) });
const hasHead = value => Boolean(value.head_message_id || value.head_sent_at);

export function coordinateAcquisition(history, catchup) {
  let running = null;
  const pending = [];
  const queue = (trigger) => {
    if (running && trigger === 'alarm') return running;
    if (!pending.includes(trigger)) pending.push(trigger);
    if (running) return running;
    running = (async () => {
      while (pending.length > 0) {
        const next = pending.shift();
        await catchup.wake(next);
        await history.wake();
      }
    })().finally(() => { running = null; });
    return running;
  };
  return {
    catchup,
    initial: history,
    wake(trigger = 'alarm') {
      return queue(trigger);
    },
    async reportCaptureState() {
      history.cancelCurrent?.('Capture state changed');
      return catchup.reportCaptureState();
    },
    requestCaptureStateReport() {
      return catchup.requestCaptureStateReport?.() ?? Promise.resolve();
    },
    onIngestAcknowledged(payload) {
      const resume = () => catchup.onIngestAcknowledged?.(payload);
      return running ? running.then(resume) : resume();
    },
    cancelCurrent(reason) { catchup.cancelCurrent(); return history.cancelCurrent(reason); },
    stop() { catchup.stop(); history.stop(); },
    historyErrorCode: () => history.historyErrorCode(),
  };
}

export class CatchupCoordinator {
  constructor({ outbox, signer, configuration, session, rpc, captureState, workerInstanceId = crypto.randomUUID(),
    clock = Date.now, delay = ms => new Promise(resolve => setTimeout(resolve, ms)), dailyCap = 1000 }) {
    Object.assign(this, { outbox, signer, configuration, session, rpc, captureState, workerInstanceId, clock, delay });
    this.allowance = new AcquisitionAllowance({ outbox, clock, dailyCap });
    this.running = null;
    this.notificationReporting = null;
    this.notificationReportPending = false;
    this.reportLock = null;
    this.controller = new AbortController();
    this.pendingTriggers = [];
    this.stopped = false;
    this.renewed = null;
    this.leaseToken = crypto.randomUUID();
  }

  #enabled() {
    return this.configuration()?.history_acquisition?.enabled === true;
  }

  wake(trigger = 'alarm') {
    if (this.stopped) return Promise.resolve();
    if (!this.#enabled()) { this.cancelCurrent(); return Promise.resolve(); }
    if (trigger === 'alarm') {
      if (this.reporting) return this.reporting;
      if (this.running) return this.running;
      if (this.pendingTriggers.length > 0) return this.#ensureRunning();
    }
    if (!this.pendingTriggers.includes(trigger)) this.pendingTriggers.push(trigger);
    if (this.reporting) return this.reporting.then(() => this.#ensureRunning());
    return this.#ensureRunning();
  }

  #ensureRunning() {
    if (this.stopped) return Promise.resolve();
    if (this.running) return this.running;
    this.running = (async () => {
      while (!this.stopped && this.pendingTriggers.length > 0) {
        const trigger = this.pendingTriggers.shift();
        this.controller = new AbortController();
        try {
          const outcome = await this.#run(trigger);
          if (outcome?.wait_for_ack === true) {
            if (!this.pendingTriggers.includes(outcome.trigger)) this.pendingTriggers.unshift(outcome.trigger);
            break;
          }
        } catch {}
      }
    })().finally(() => { this.running = null; });
    return this.running;
  }

  onIngestAcknowledged() {
    if (!this.#enabled()) return Promise.resolve();
    return this.pendingTriggers.length > 0 ? this.#ensureRunning() : Promise.resolve();
  }

  cancelCurrent() { this.controller?.abort(); this.renewed = null; clearTimeout(this.renewTimer); }
  stop() { this.stopped = true; this.cancelCurrent(); }

  reportCaptureState() {
    if (!this.#enabled()) { this.cancelCurrent(); return Promise.resolve(); }
    if (this.reporting) return this.reporting;
    this.cancelCurrent();
    this.reporting = (async () => {
      await this.running;
      this.controller = new AbortController();
      await this.#report(await this.captureState());
    })().finally(() => { this.reporting = null; });
    return this.reporting.then(() => this.#ensureRunning());
  }

  requestCaptureStateReport() {
    if (!this.#enabled()) return Promise.resolve();
    if (this.notificationReporting) {
      this.notificationReportPending = true;
      return this.notificationReporting;
    }
    this.notificationReportPending = false;
    this.notificationReporting = (async () => {
      if (this.running) await this.running;
      if (this.reporting) await this.reporting;
      do {
        this.notificationReportPending = false;
        await this.#report(await this.captureState());
      } while (this.notificationReportPending);
    })().finally(() => { this.notificationReporting = null; });
    return this.notificationReporting;
  }

  #identity() {
    const config = this.configuration();
    const session = this.session();
    return JSON.stringify([config?.creator_account_id, config?.config_revision,
      config?.history_acquisition?.consent_revision, config?.history_acquisition?.authorized_platform_creator_id,
      session?.creator_account_id, session?.connection_id, session?.fencing_token,
      this.outbox.identityState().account_epoch]);
  }

  async #assert(expected) {
    this.controller.signal.throwIfAborted();
    if (expected !== this.#identity()) throw failure('authorization_changed');
    const config = this.configuration();
    const session = this.session();
    const state = await this.captureState();
    if (!session || config?.creator_account_id !== session.creator_account_id
      || config.config_revision !== session.applied_config_revision || !config.history_acquisition?.enabled
      || blocked.has(state.reason) || state.runnable === false) throw failure(state.reason === 'account_mismatch' ? 'account_changed'
        : state.reason === 'paused' ? 'paused' : 'authorization_changed');
    this.controller.signal.throwIfAborted();
  }

  async #rpc(operation, payload) {
    const now = this.clock();
    let permitted = false;
    await this.allowance.update(state => {
      if (now < (state.rpc_not_before ?? 0)) return;
      state.rpc_failures = (state.rpc_failures ?? 0) + 1;
      state.rpc_not_before = now + Math.min(3_600_000, 60_000 * 2 ** Math.min(6, state.rpc_failures - 1));
      permitted = true;
    });
    if (!permitted) throw failure('rpc_backoff');
    const session = this.session();
    const value = await this.rpc(operation, { ...payload, operation, protocol_version: '2',
      auth_ticket: session.config_auth_ticket, agent_installation_id: session.agent_installation_id,
      creator_account_id: session.creator_account_id }, { signal: this.controller.signal });
    const parsed = operation === 'capture.state.report' ? parseCaptureStateReportResponse(value) : parseHistoryCheckBeginResponse(value);
    if (operation === 'capture.state.report' && parsed.acknowledged_seq !== payload.report_seq) throw failure('rpc_ack');
    await this.allowance.update(state => { state.rpc_failures = 0; state.rpc_not_before = 0; });
    return parsed;
  }

  async #report(state) {
    while (this.reportLock !== null) await this.reportLock;
    let release;
    this.reportLock = new Promise(resolve => { release = resolve; });
    try {
      let next = state;
      while (next) next = await this.#reportOnce(next);
    } finally {
      this.reportLock = null;
      release();
    }
  }

  async #reportOnce(state) {
    const now = this.clock();
    const day = new Date(now).toISOString().slice(0, 10);
    const summary = { observing: state.observing === true, reason: state.reason,
      tabs: state.tabs, page_socket_open: state.page_socket_open === true };
    const control = await this.allowance.update(saved => {
      saved.days ??= {};
      saved.days[day] ??= { requests: emptyCounters(), automatic: 0 };
      saved.drop_total ??= { expired: 0, rejected: 0 };
      if (state.drop_sources) {
        saved.drop_documents ??= {};
        for (const [tab, counts] of Object.entries(state.drop_sources)) {
          const previous = saved.drop_documents[tab]?.document === counts.document
            ? saved.drop_documents[tab] : { expired: 0, rejected: 0 };
          for (const key of ['expired', 'rejected']) saved.drop_total[key] += Math.max(0, counts[key] - previous[key]);
          saved.drop_documents[tab] = copy(counts);
        }
        for (const tab of Object.keys(saved.drop_documents)) {
          if (!state.drop_tabs.includes(Number(tab))) delete saved.drop_documents[tab];
        }
      } else {
        if (saved.drop_worker !== this.workerInstanceId) {
          saved.drop_worker = this.workerInstanceId;
          saved.drop_seen = { expired: 0, rejected: 0 };
        }
        for (const key of ['expired', 'rejected']) {
          const count = state.drops?.[key] ?? 0;
          saved.drop_total[key] += Math.max(0, count - saved.drop_seen[key]);
          saved.drop_seen[key] = count;
        }
      }
      if (saved.pending_report) return;
      const fingerprint = JSON.stringify([summary, saved.drop_total,
        Object.fromEntries(Object.entries(saved.days).map(([day, entry]) => [day, entry.requests]))]);
      if (saved.report_worker === this.workerInstanceId && saved.report_signature === fingerprint
        && now - saved.report_at < 60_000) return;
      if (saved.report_worker !== this.workerInstanceId) { saved.report_seq = 0; saved.report_worker = this.workerInstanceId; }
      const reportDay = Object.keys(saved.days).sort().find(d => {
        const entry = saved.days[d];
        return Object.keys(entry.requests).some(k => entry.requests[k] > (entry.acked?.[k] ?? 0));
      }) ?? day;
      const daily = saved.days[reportDay];
      saved.pending_report = { worker_instance_id: this.workerInstanceId, report_seq: ++saved.report_seq,
        ...summary, utc_day: reportDay, automatic_pages_today: daily.automatic,
        drops_since_last: Object.fromEntries(['expired', 'rejected'].map(k => [k, saved.drop_total[k] - (saved.drop_acked?.[k] ?? 0)])),
        requests_since_last: Object.fromEntries(Object.keys(daily.requests).map(k => [k, daily.requests[k] - (daily.acked?.[k] ?? 0)])) };
      saved.pending_totals = { requests: copy(daily.requests), drops: copy(saved.drop_total), signature: fingerprint };
    });
    if (!control.pending_report || !this.session()) return;
    await this.#rpc('capture.state.report', control.pending_report);
    await this.allowance.update(saved => {
      const day = saved.pending_report.utc_day;
      saved.days[day].acked = saved.pending_totals.requests;
      saved.drop_acked = saved.pending_totals.drops;
      saved.report_signature = saved.pending_totals.signature;
      saved.report_at = this.clock();
      saved.pending_report = null;
      saved.pending_totals = null;
      for (const old of Object.keys(saved.days)) {
        const entry = saved.days[old];
        if (old < new Date(this.clock()).toISOString().slice(0, 10)
          && Object.keys(entry.requests).every(k => entry.requests[k] === entry.acked?.[k])) delete saved.days[old];
      }
    });
    const pending = control.pending_report;
    if (pending.worker_instance_id !== this.workerInstanceId || pending.observing !== summary.observing
      || pending.reason !== summary.reason || pending.page_socket_open !== summary.page_socket_open
      || JSON.stringify(pending.tabs) !== JSON.stringify(summary.tabs)) return this.captureState();
    return null;
  }

  async #begin(job, trigger) {
    const active = terminal(job) ? null : job.check_id;
    const control = await this.allowance.update(saved => { saved.begin_request_id ??= crypto.randomUUID(); });
    const requestId = active ? job.request_id : control.begin_request_id;
    const response = await this.#rpc('history.check.begin', { request_id: requestId,
      worker_instance_id: this.workerInstanceId, trigger: active ? 'renew' : trigger,
      config_revision: this.configuration().config_revision, head_evidence: 'none', active_check_id: active });
    if (response.result !== 'granted') {
      await this.allowance.update(state => { state.check_not_before = this.clock() + (response.retry_after_seconds ?? 60) * 1000;
        state.begin_request_id = null; });
      return null;
    }
    if (job?.check_id === response.check_id && terminal(job)) return null;
    if (job?.check_id !== response.check_id) {
      job = { job_id: ACTIVE, kind: 'catchup', schema_version: 1, check_id: response.check_id,
        generation_id: response.check_id, request_id: requestId, phase: 'inventory',
        account_epoch: this.outbox.identityState().account_epoch, creator_account_id: this.session().creator_account_id,
        authorization_revision: this.configuration().history_acquisition.consent_revision,
        authorized_platform_creator_id: this.configuration().history_acquisition.authorized_platform_creator_id,
        cursor: null, boundary: null, seen_cursors: [], seen: [], queue: [], frozen: {}, passive_heads: {},
        changed: 0, movers: 0, mover_passes: 0, counts: { list: 0, messages: 0, probes: 0 },
        heads: [], strategies: [], final_source_seq: 0, retries: 0,
        B: Date.parse(response.uncertain_since ?? response.granted_at) - 900_000 };
    }
    Object.assign(job, { grant: response, gap_epoch: response.gap_epoch, lease_token: this.leaseToken,
      grant_used: 0, renew_at: this.clock() + (Date.parse(response.lease_expires_at) - this.clock()) / 2 });
    await this.outbox.saveHistoryJob(job);
    await this.allowance.update(saved => { saved.begin_request_id = null; saved.check_not_before = 0; });
    this.renewed = job.check_id;
    clearTimeout(this.renewTimer);
    this.renewTimer = setTimeout(() => { void this.wake('renew'); }, Math.max(1, job.renew_at - this.clock()));
    this.renewTimer.unref?.();
    return job;
  }

  async #commit(job, { changes = [], evidence = [], patch = {}, cursor = job.cursor } = {}, expected = null) {
    const result = await this.outbox.commitPage({ jobId: ACTIVE, expectedAccountEpoch: job.account_epoch,
      expectedLeaseToken: this.leaseToken, checkId: job.check_id, changes, evidence,
      nextCursor: cursor, jobPatch: patch,
      ...(expected === null ? {} : { validateAuthorization: () => this.#assert(expected),
        signal: this.controller.signal, assertCurrent: () => this.controller.signal.throwIfAborted() }) });
    if (terminal(result.job)) clearTimeout(this.renewTimer);
    return result;
  }

  async #abandon(job, reason) {
    if (terminal(job)) return;
    await this.#commit(job, { evidence: [{ type: 'check.abandoned', generation_id: job.check_id, reason }],
      patch: { phase: 'abandoned' } });
  }

  async #run(trigger) {
    if (!this.session()) return;
    const state = await this.captureState();
    await this.allowance.update(saved => {
      if (trigger === 'observing' && !state.observing) trigger = 'alarm';
      if (trigger === 'tab_runnable' && !state.runnable) trigger = 'alarm';
      if (trigger !== 'admission' && state.runnable && saved.was_runnable === false) trigger = 'tab_runnable';
      else if (trigger !== 'admission' && state.observing && saved.was_observing === false) trigger = 'observing';
      saved.was_runnable = state.runnable;
      saved.was_observing = state.observing;
    });
    await this.#report(state);
    let job = await this.outbox.historyJob(ACTIVE);
    const expected = this.#identity();
    try { await this.#assert(expected); }
    catch { if (job?.lease_token === this.leaseToken) await this.#abandon(job, state.reason === 'paused' ? 'paused' : 'authorization_changed'); return; }
    if (terminal(job) && job && this.outbox.identityState().acknowledged_source_seq < job.final_source_seq) {
      return ['admission', 'observing', 'tab_runnable'].includes(trigger)
        ? { wait_for_ack: true, trigger } : undefined;
    }
    const control = await this.allowance.update(() => {});
    if (terminal(job) || this.renewed !== job.check_id || this.clock() >= job.renew_at || job.grant_used >= job.grant.page_budget) {
      if (this.clock() < (control.check_not_before ?? 0)) return;
      job = await this.#begin(job, trigger);
      if (!job) return;
    }
    const deadline = this.clock() + 20_000;
    const timer = setTimeout(() => this.controller.abort(), 20_000);
    try {
      while (!terminal(job) && this.clock() < deadline) {
        await this.#assert(expected);
        if (this.clock() >= job.renew_at) {
          await this.#report(await this.captureState());
          job = await this.#begin(job, 'renew');
          if (!job) break;
        }
        if (job.phase === 'messages' && job.queue.length === 0) {
          const movers = Object.keys(job.passive_heads).filter(id => !job.seen.includes(id) && !job.frozen[id]);
          if (job.grant.kind === 'catch_up' && movers.length) {
            if (job.mover_passes >= 3) { await this.#abandon(job, 'retry_exhausted'); break; }
            job.mover_passes++;
            for (const id of movers) {
              job.queue.push(id);
              job.frozen[id] = { target: job.passive_heads[id], cursor: null, seen_cursors: [], probe: false };
            }
            job.movers += movers.length;
            await this.outbox.saveHistoryJob(job);
          } else {
            const evidence = [];
            if (job.grant.kind === 'catch_up') evidence.push({ type: 'check.inventory_closed', generation_id: job.check_id,
              strategy: job.strategies.length > 1 ? 'mixed' : job.strategies[0] ?? 'probe',
              scanned: job.seen.length, changed: job.changed, movers: job.movers });
            evidence.push((seq, current) => {
              if (job.grant.kind === 'catch_up' && Object.keys(current.passive_heads ?? {})
                .some(id => !job.seen.includes(id) && !job.frozen[id])) throw failure('mover_pending');
              return { type: 'check.completed', generation_id: job.check_id, kind: job.grant.kind,
                final_source_seq: seq, pages_read: Object.values(job.counts).reduce((a, b) => a + b, 0), counts: job.counts,
                ...(job.grant.kind === 'canary' ? { heads: job.heads } : {}) };
            });
            try { await this.#commit(job, { evidence, patch: { phase: 'completed' } }, expected); }
            catch (error) { if (error.code !== 'mover_pending') throw error; }
            break;
          }
        }
        if (this.clock() < (job.retry_at ?? 0)) break;
        if (job.grant_used >= job.grant.page_budget) break;
        const inventory = job.phase === 'inventory';
        const chatId = job.queue[0];
        const chat = job.frozen[chatId];
        const probe = !inventory && chat.probe && chat.cursor === null;
        const category = inventory ? job.grant.kind === 'canary' ? 'canary_list' : 'catchup_list' : 'catchup_messages';
        const policy = this.configuration().history_acquisition;
        if (!await this.allowance.reserve(category, policy.pages_per_wake, { automatic: true, retry: job.retries > 0 })) break;
        const count = inventory ? 'list' : probe ? 'probes' : 'messages';
        job.counts[count]++;
        job.grant_used++;
        await this.outbox.saveHistoryJob(job);
        try {
          const operation = inventory ? 'conversations' : 'message-page';
          const cursor = inventory ? job.cursor : chat.cursor;
          const signal = this.controller.signal;
          let cancel;
          const interrupted = new Promise((resolve, reject) => {
            cancel = () => reject(signal.reason);
            signal.addEventListener('abort', cancel, { once: true });
          });
          const result = await Promise.race([interrupted, this.signer.read({ operation, refreshMode: 'allow', signal,
            parameters: { ...(inventory ? {} : { conversationId: chatId }), query: { limit: policy.page_size, cursor } } })])
            .finally(() => signal.removeEventListener('abort', cancel));
          await this.#assert(expected);
          const data = result?.data;
          if (result?.success !== true) throw Object.assign(failure('read_failed'), { retryAfter: result?.response?.retry_after_ms });
          if (!data || !Array.isArray(data.items) || data.items.length > policy.page_size
            || (data.continuation === null ? data.boundary !== (inventory ? 'inventory_end' : 'history_start')
              : data.boundary !== null || typeof data.continuation !== 'string' || !SAFE_CURSOR.test(data.continuation)
                || data.continuation.length > MAX_CURSOR_LENGTH || data.items.length === 0)) throw failure('cursor_invalid');
          const seen = inventory ? job.seen_cursors : chat.seen_cursors;
          if (data.continuation !== null && (data.continuation === cursor || seen.includes(data.continuation))) throw failure('cursor_invalid');
          const current = await this.outbox.historyJob(ACTIVE);
          job.passive_heads = current.passive_heads;
          if (data.continuation !== null) seen.push(data.continuation);
          const context = { observedAt: new Date(this.clock()).toISOString(), creatorPlatformId: policy.authorized_platform_creator_id, conversationId: chatId };
          let changes;
          const evidence = [];
          if (inventory) {
            changes = data.items.map(item => {
              const { head_message_id, head_sent_at, ...material } = item;
              return normalizeSignerConversation(material, context);
            });
            for (const item of data.items) {
              if (job.seen.includes(item.id)) continue;
              job.seen.push(item.id);
              const known = hasHead(item);
              if (job.grant.kind === 'canary') {
                if (known) {
                  if (!item.head_sent_at || Date.parse(item.head_sent_at) <= Date.parse(job.grant.granted_at) - 120_000)
                    job.heads.push({ chat_id: item.id, ...(item.head_message_id ? { head_message_id: item.head_message_id } : {}),
                      ...(item.head_sent_at ? { head_sent_at: item.head_sent_at } : {}) });
                } else if (job.queue.length < 3) {
                  job.queue.push(item.id);
                  job.frozen[item.id] = { target: {}, cursor: null, seen_cursors: [], probe: true };
                }
              } else if (!item.head_sent_at || Date.parse(item.head_sent_at) >= job.B) {
                job.queue.push(item.id);
                job.frozen[item.id] = { target: head(item), cursor: null, seen_cursors: [], probe: !known };
                job.changed++;
              }
              const strategy = known ? 'timestamp' : 'probe';
              if (!job.strategies.includes(strategy)) job.strategies.push(strategy);
            }
            if (data.continuation === null || job.grant.kind === 'canary') job.phase = 'messages';
          } else {
            changes = data.items.map(item => normalizeSignerMessage(item, context));
            if (!Object.keys(chat.target).length && data.items[0]) chat.target = { message_id: data.items[0].id, sent_at: data.items[0].sent_at };
            const reached = data.items.some(item => Date.parse(item.sent_at) < job.B) ? 'boundary'
              : data.boundary === 'history_start' ? 'history_start' : null;
            if (job.grant.kind === 'canary') {
              if (chat.target.sent_at && Date.parse(chat.target.sent_at) <= Date.parse(job.grant.granted_at) - 120_000)
                job.heads.push({ chat_id: chatId, head_message_id: chat.target.message_id, head_sent_at: chat.target.sent_at });
              job.queue.shift();
            } else if (reached) {
              evidence.push(seq => ({ type: 'check.chat_reconciled', generation_id: job.check_id, chat_id: chatId,
                target_head: chat.target, reached, final_source_seq: seq }));
              job.queue.shift();
            }
            chat.cursor = data.continuation;
          }
          const resultCommit = await this.#commit(job, { changes, evidence,
            cursor: inventory ? data.continuation : job.cursor, patch: { ...job, retries: 0, retry_at: null } }, expected);
          job = resultCommit.job;
        } catch (error) {
          job = await this.outbox.historyJob(ACTIVE);
          if (this.controller.signal.aborted) break;
          if (['paused', 'account_changed', 'authorization_changed', 'cursor_invalid'].includes(error.code)) {
            await this.#abandon(job, error.code); break;
          }
          const invariant = ['material_conflict', 'identity_conflict', 'tombstone_revive', 'evidence_conflict'].includes(error.code);
          job.retries++;
          if (job.retries > (invariant ? 1 : policy.retry_limit)) { await this.#abandon(job, 'retry_exhausted'); break; }
          job.retry_at = this.clock() + Math.min(3_600_000, Math.max(1000 * 2 ** (job.retries - 1), error.retryAfter ?? 0));
          await this.outbox.saveHistoryJob(job);
          break;
        }
        if (policy.request_interval_ms > 0) await this.delay(policy.request_interval_ms);
      }
    } finally { clearTimeout(timer); }
    job = await this.outbox.historyJob(ACTIVE);
    if (!terminal(job) && !this.controller.signal.aborted) {
      const saved = await this.allowance.update(() => {});
      const day = new Date(this.clock()).toISOString().slice(0, 10);
      if ((saved.days?.[day]?.automatic ?? 0) >= this.allowance.dailyCap || job.grant_used >= job.grant.page_budget) {
        await this.#report(await this.captureState());
        await this.#begin(job, 'renew');
      }
    }
  }
}
