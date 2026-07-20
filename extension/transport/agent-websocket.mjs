import {
  parseAgentToBrainMessage,
  parseBrainToAgentMessage,
} from '../protocol/index.mjs';
import { AgentCommandService } from './agent-command-service.mjs';

const CONNECTING = 0;
const OPEN = 1;
export const LEASE_EXPIRED_CLOSE_CODE = 4001;

// The WebSocket API accepts only close code 1000 or the 3000-4999 range; passing
// a reserved code (1002/1008/1011) throws InvalidAccessError and leaves the
// socket to flap. Map reserved codes into the application range.
function safeCloseCode(code) {
  return code === 1000 || (code >= 3000 && code <= 4999) ? code : 4000 + (code % 1000);
}

const defaultScheduler = {
  setTimeout: (handler, delay) => setTimeout(handler, delay),
  clearTimeout: (handle) => clearTimeout(handle),
  setInterval: (handler, delay) => setInterval(handler, delay),
  clearInterval: (handle) => clearInterval(handle),
};

const noOp = () => {};
const asyncNoOp = async () => {};

export class AgentWebSocketClient {
  constructor(options) {
    this.url = options.url ?? 'ws://bridge.localhost:17871/ws/agent';
    this.identity = options.identity;
    if (typeof options.creatorAccountId !== 'string' || options.creatorAccountId.length === 0) {
      throw new Error('A Brain-authorized creatorAccountId is required');
    }
    if (typeof options.authTicket !== 'string' || options.authTicket.length === 0) {
      throw new Error('A Brain authTicket is required');
    }
    this.creatorAccountId = options.creatorAccountId;
    this.authTicket = options.authTicket;
    if (
      options.reconnectAuthTicket !== undefined
      && options.reconnectAuthTicket !== null
      && (typeof options.reconnectAuthTicket !== 'string' || options.reconnectAuthTicket.length === 0)
    ) throw new Error('The Agent reconnect auth ticket is invalid');
    this.reconnectAuthTicket = options.reconnectAuthTicket ?? null;
    this.persistReconnectAuthTicket = options.persistReconnectAuthTicket ?? asyncNoOp;
    this.bootstrapAuthTicketUsed = false;
    this.extensionVersion = options.extensionVersion ?? '2.0.0';
    this.capabilities = options.capabilities ?? [
      'capture.chats',
      'capture.messages',
      'capture.presence',
      'history.sync',
      'command.message.send',
    ];
    this.webSocketFactory = options.webSocketFactory ?? ((url) => new WebSocket(url));
    this.scheduler = options.scheduler ?? defaultScheduler;
    this.idFactory = options.idFactory ?? (() => crypto.randomUUID());
    this.random = options.random ?? Math.random;
    this.now = options.now ?? (() => Date.now());
    this.reconnectBaseMs = options.reconnectBaseMs ?? 500;
    this.reconnectMaxMs = options.reconnectMaxMs ?? 30_000;
    this.persistence = options.persistence ?? {};
    this.outbox = options.outbox ?? null;
    this.onSession = options.onSession ?? noOp;
    this.onSessionLost = options.onSessionLost ?? noOp;
    this.onSyncRequired = options.onSyncRequired ?? noOp;
    this.onIngestAcknowledged = options.onIngestAcknowledged ?? noOp;
    this.onIngestRejected = options.onIngestRejected ?? noOp;
    this.onConfigAvailable = options.onConfigAvailable ?? noOp;
    this.configClient = options.configClient ?? null;
    const executor = options.executor ?? (
      options.onCommand
        ? { execute: (action) => options.onCommand(action) }
        : undefined
    );
    const appliedConfig = options.appliedConfig ?? (() => {
      if (this.configClient?.activeDocument) return this.configClient.activeDocument;
      if (options.onCommand) {
        return {
          command_policy: {
            allowed_actions: ['message.send'],
            max_text_length: Number.MAX_SAFE_INTEGER,
            require_idempotency: true,
          },
        };
      }
      return null;
    });
    this.commandService = options.commandService ?? new AgentCommandService({
      persistence: this.persistence,
      executor,
      appliedConfig,
      now: this.now,
      idFactory: this.idFactory,
    });
    this.onCommandResultAcknowledged = options.onCommandResultAcknowledged ?? noOp;
    this.onProtocolError = options.onProtocolError ?? noOp;
    this.onValidationError = options.onValidationError ?? noOp;
    this.health = options.health ?? (() => ({ status: 'healthy', detail: null }));
    this.socket = null;
    this.session = null;
    this.reconnectTimer = null;
    this.heartbeatTimer = null;
    this.heartbeatIntervalMs = null;
    this.lastHeartbeatSentAt = null;
    this.reconnectAttempt = 0;
    this.stopped = true;
    this.reconnectAllowed = true;
    this.syncRequired = false;
    this.sentSourceSeqs = new Set();
    this.flushPromise = null;
    this.snapshotSendPromise = null;

  }

  start() {
    this.stopped = false;
    this.reconnectAllowed = true;
    this.ensureConnected();
  }

  ensureConnected() {
    if (this.stopped) this.stopped = false;
    if (this.socket && [CONNECTING, OPEN].includes(this.socket.readyState)) return;
    this.clearReconnect();
    this.openSocket();
  }

  replaceAuthTicket(authTicket) {
    if (typeof authTicket !== 'string' || authTicket.length === 0) {
      throw new Error('A Brain authTicket is required');
    }
    this.authTicket = authTicket;
  }

  reconcileConnection() {
    if (this.socket?.readyState === OPEN && this.session !== null) {
      return this.sendHeartbeatIfDue();
    }
    this.ensureConnected();
    return false;
  }

  stop() {
    this.stopped = true;
    this.reconnectAllowed = false;
    this.clearReconnect();
    this.clearHeartbeat();
    const socket = this.socket;
    this.socket = null;
    this.session = null;
    this.configClient?.clearSessionAuthorization?.();
    this.onSessionLost({ reason: 'stopped' });
    socket?.close(1000, 'Agent stopped');
  }

  sendSnapshot(snapshot) {
    return this.sendBound('ingest.snapshot', {
      ...snapshot,
      agent_installation_id: this.identity.agentInstallationId,
      agent_stream_id: this.identity.agentStreamId,
    });
  }

  sendDelta(delta) {
    if (this.outbox === null) {
      return this.sendBound('ingest.delta', {
        ...delta,
        agent_installation_id: this.identity.agentInstallationId,
        agent_stream_id: this.identity.agentStreamId,
      });
    }
    return this.captureDelta(delta.change ?? delta, delta.event_id ?? null);
  }

  async captureDelta(change, eventId = null) {
    if (change?.type === 'message.upsert') {
      throw new Error('message.upsert capture requires captureMessageWithParent');
    }
    const item = await this.outbox.enqueue(change, eventId ?? this.idFactory());
    await this.flushOutbox();
    return item;
  }

  async captureMessageWithParent(messageChange, parentChange) {
    if (this.outbox === null) {
      throw new Error('Dependency-closed capture requires a durable outbox');
    }
    const result = await this.outbox.enqueueMessageWithParent(messageChange, parentChange);
    await this.flushOutbox();
    return result.messageItem;
  }

  async flushOutbox() {
    if (
      this.outbox === null ||
      this.syncRequired ||
      !this.session ||
      !this.socket ||
      this.socket.readyState !== OPEN
    ) return false;
    if (this.flushPromise !== null) {
      await this.flushPromise;
      return this.flushOutbox();
    }
    this.flushPromise = (async () => {
      let after = this.identity.lastAcknowledgedSourceSeq;
      while (!this.syncRequired) {
        const entries = typeof this.outbox.entriesPage === 'function'
          ? await this.outbox.entriesPage(after, 100)
          : await this.outbox.entries();
        for (const item of entries) {
          if (this.syncRequired || this.sentSourceSeqs.has(item.source_seq)) continue;
          const sent = this.sendBound('ingest.delta', {
            event_id: item.event_id,
            source_seq: item.source_seq,
            acquisition_origin: item.acquisition_origin ?? 'passive',
            change: item.change,
            agent_installation_id: this.identity.agentInstallationId,
            agent_stream_id: this.identity.agentStreamId,
          });
          if (!sent) return;
          this.sentSourceSeqs.add(item.source_seq);
        }
        if (entries.length < 100 || typeof this.outbox.entriesPage !== 'function') break;
        after = entries.at(-1).source_seq;
      }
    })();
    try {
      await this.flushPromise;
      return true;
    } finally {
      this.flushPromise = null;
    }
  }

  sendPresenceObservation(observation) {
    return this.sendBound('presence.observed', observation);
  }

  sendConfigApplied(report) {
    return this.sendBound('config.applied', report);
  }

  sendCommandResult(result, correlationId = null) {
    return this.sendBound('command.result', result, correlationId);
  }

  sendHeartbeat() {
    const sent = this.sendBound('agent.heartbeat', {
      applied_config_revision: this.identity.appliedConfigRevision,
      health: this.health(),
    });
    if (sent) this.lastHeartbeatSentAt = this.now();
    return sent;
  }

  sendHeartbeatIfDue() {
    if (
      this.heartbeatIntervalMs === null
      || (
        this.lastHeartbeatSentAt !== null
        && this.now() - this.lastHeartbeatSentAt < this.heartbeatIntervalMs
      )
    ) return false;
    return this.sendHeartbeat();
  }

  sendBound(type, payload, correlationId = null) {
    if (!this.session || !this.socket || this.socket.readyState !== OPEN) return false;
    const document = {
      type,
      protocol_version: '2',
      message_id: this.idFactory(),
      ...(correlationId ? { correlation_id: correlationId } : {}),
      payload: {
        ...payload,
        connection_id: this.session.connection_id,
        fencing_token: this.session.fencing_token,
        creator_account_id: this.session.creator_account_id,
      },
    };
    const validated = parseAgentToBrainMessage(document);
    this.socket.send(JSON.stringify(validated));
    return true;
  }

  openSocket() {
    const socket = this.webSocketFactory(this.url);
    this.socket = socket;
    this.session = null;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      const authTicket = this.reconnectAuthTicket ?? (
        this.bootstrapAuthTicketUsed ? null : this.authTicket
      );
      if (authTicket === null) {
        this.reconnectAllowed = false;
        this.onValidationError(new Error('No reusable Agent reconnect credential is available'));
        socket.close(safeCloseCode(1008), 'Agent reconnect credential unavailable');
        return;
      }
      if (this.reconnectAuthTicket === null) this.bootstrapAuthTicketUsed = true;
      const hello = {
        type: 'agent.hello',
        protocol_version: '2',
        message_id: this.idFactory(),
        payload: {
          auth_ticket: authTicket,
          agent_installation_id: this.identity.agentInstallationId,
          requested_creator_account_id: this.creatorAccountId,
          capabilities: this.capabilities,
          extension_version: this.extensionVersion,
          agent_stream_id: this.identity.agentStreamId,
          last_acknowledged_source_seq: this.identity.lastAcknowledgedSourceSeq,
          applied_config_revision: this.identity.appliedConfigRevision,
        },
      };
      socket.send(JSON.stringify(parseAgentToBrainMessage(hello)));
    };
    socket.onmessage = (event) => this.handleRawMessage(socket, event.data);
    socket.onerror = noOp;
    socket.onclose = () => this.handleClose(socket);
  }

  handleRawMessage(socket, raw) {
    if (socket !== this.socket) return;
    let decoded;
    try {
      decoded = JSON.parse(String(raw));
    } catch (error) {
      this.onValidationError(error);
      socket.close(safeCloseCode(1002), 'Malformed JSON from Brain');
      return;
    }
    let message;
    try {
      message = parseBrainToAgentMessage(decoded);
    } catch (error) {
      this.onValidationError(error);
      socket.close(safeCloseCode(1002), 'Invalid protocol frame from Brain');
      return;
    }

    if (message.type === 'protocol.error') {
      this.onProtocolError(message.payload);
      if (message.payload.fatal) {
        this.reconnectAllowed = message.payload.retryable;
        socket.close(safeCloseCode(1002), message.payload.code);
      }
      return;
    }
    if (!this.session) {
      if (message.type !== 'agent.session') {
        socket.close(safeCloseCode(1002), 'Expected agent.session');
        return;
      }
      this.acceptSession(message.payload);
      return;
    }
    if (message.type === 'agent.session') {
      socket.close(safeCloseCode(1002), 'Duplicate agent.session');
      return;
    }
    if (message.type === 'command.execute') {
      this.dispatch(message).catch((error) => this.onValidationError(error));
      return;
    }
    if (!this.matchesSession(message)) {
      this.reconnectAllowed = false;
      socket.close(safeCloseCode(1008), 'Session identity conflict');
      return;
    }
    this.dispatch(message).catch((error) => this.onValidationError(error));
  }

  acceptSession(session) {
    if (
      session.creator_account_id !== this.creatorAccountId ||
      session.agent_installation_id !== this.identity.agentInstallationId ||
      session.agent_stream_id !== this.identity.agentStreamId
    ) {
      this.reconnectAllowed = false;
      this.socket?.close(safeCloseCode(1008), 'Session identity conflict');
      return;
    }
    this.reconnectAuthTicket = session.reconnect_auth_ticket;
    void Promise.resolve(this.persistReconnectAuthTicket(session.reconnect_auth_ticket))
      .catch((error) => {
        this.reconnectAllowed = false;
        this.onValidationError(error);
        this.socket?.close(safeCloseCode(1011), 'Agent reconnect credential could not be stored');
      });
    this.session = session;
    this.configClient?.bindSessionAuthorization?.(session.config_auth_ticket);
    this.sentSourceSeqs.clear();
    this.syncRequired = session.resume_action === 'snapshot_required';
    this.reconnectAttempt = 0;
    this.startHeartbeat(session.lease.heartbeat_interval_seconds * 1000);
    this.lastHeartbeatSentAt = this.now();
    this.onSession(session);
    void this.resendCommandResults()
      .catch((error) => this.onValidationError(error));
    if (
      this.configClient !== null
      && session.required_config_revision !== this.identity.appliedConfigRevision
    ) {
      void this.configClient.requireConfig({
        required_config_revision: session.required_config_revision,
        digest: null,
      });
    }
    if (session.resume_action === 'snapshot_required') {
      void this.handleSyncRequired({
        connection_id: session.connection_id,
        creator_account_id: session.creator_account_id,
        reason: 'missing_checkpoint',
        expected_agent_stream_id: session.agent_stream_id,
        expected_next_source_seq: session.committed_source_seq + 1,
        pending_snapshot_id: session.pending_snapshot_id,
        next_expected_chunk_index: session.next_expected_chunk_index,
        snapshot: {
          include_chats: true,
          include_messages: true,
          include_coverage_evidence: true,
          max_frame_bytes: 524_288,
          max_records_per_chunk: 100,
        },
      }).catch((error) => this.onValidationError(error));
    } else {
      void this.reconcileLocalSnapshot(session)
        .then(() => this.flushOutbox())
        .catch((error) => this.onValidationError(error));
    }
  }

  async handleSyncRequired(payload) {
    this.syncRequired = true;
    this.onSyncRequired(payload);
    if (this.outbox === null) return;
    const manifest = await this.outbox.prepareSnapshot();
    const canResume = payload.pending_snapshot_id === manifest.snapshot_id;
    if (canResume) {
      await this.sendNextSnapshotFrame(payload.next_expected_chunk_index);
      return;
    }
    const begin = await this.outbox.snapshotBeginFrame();
    this.sendSnapshot(begin);
  }

  async reconcileLocalSnapshot(session) {
    if (this.outbox === null || typeof this.outbox.currentSnapshotManifest !== 'function') return;
    const manifest = await this.outbox.currentSnapshotManifest();
    if (manifest === null || session.committed_source_seq < manifest.through_seq) return;
    await this.outbox.acknowledge(
      session.committed_source_seq,
      manifest.snapshot_id,
      {
        snapshot_id: manifest.snapshot_id,
        next_expected_chunk_index: manifest.chunk_count,
        committed: true,
      },
    );
    this.syncRequired = false;
  }

  async sendNextSnapshotFrame(nextExpectedChunkIndex) {
    if (this.snapshotSendPromise !== null) return this.snapshotSendPromise;
    this.snapshotSendPromise = (async () => {
      const manifest = await this.outbox.currentSnapshotManifest();
      if (manifest === null || manifest.state !== 'ready') {
        throw new Error('A ready local snapshot is required');
      }
      if (nextExpectedChunkIndex < manifest.chunk_count) {
        this.sendSnapshot(await this.outbox.snapshotChunkFrame(nextExpectedChunkIndex));
      } else if (nextExpectedChunkIndex === manifest.chunk_count) {
        this.sendSnapshot(await this.outbox.snapshotCommitFrame());
      } else {
        throw new Error('Brain requested a snapshot chunk beyond the manifest');
      }
    })();
    try {
      return await this.snapshotSendPromise;
    } finally {
      this.snapshotSendPromise = null;
    }
  }

  matchesSession(message) {
    const payload = message.payload;
    if (payload.connection_id !== this.session.connection_id) return false;
    if (payload.creator_account_id !== this.session.creator_account_id) return false;
    if (message.type === 'command.execute' && payload.fencing_token !== this.session.fencing_token) {
      return false;
    }
    if (message.type === 'ingest.ack' && payload.agent_stream_id !== this.identity.agentStreamId) {
      return false;
    }
    return true;
  }

  async dispatch(message) {
    switch (message.type) {
      case 'sync.required':
        await this.handleSyncRequired(message.payload);
        return;
      case 'ingest.ack': {
        let snapshotAcknowledged = false;
        if (this.outbox !== null) {
          const result = await this.outbox.acknowledge(
            message.payload.committed_source_seq,
            message.payload.snapshot_id,
            message.payload.snapshot_progress,
          );
          snapshotAcknowledged = result.snapshotAcknowledged;
        }
        const committed = Math.max(
          this.identity.lastAcknowledgedSourceSeq,
          message.payload.committed_source_seq,
        );
        this.identity.lastAcknowledgedSourceSeq = committed;
        await (this.persistence.saveAcknowledgedSourceSeq?.(committed) ?? asyncNoOp());
        this.sentSourceSeqs = new Set(
          [...this.sentSourceSeqs].filter((sourceSeq) => sourceSeq > committed),
        );
        this.onIngestAcknowledged(message.payload);
        if (snapshotAcknowledged) {
          this.syncRequired = false;
          await this.flushOutbox();
        } else if (message.payload.snapshot_progress !== null) {
          await this.sendNextSnapshotFrame(
            message.payload.snapshot_progress.next_expected_chunk_index,
          );
        }
        return;
      }
      case 'ingest.rejected':
        this.onIngestRejected(message.payload);
        if (message.payload.code === 'stale_fence') {
          this.forceReconnect('Agent lease fencing token is stale');
          return;
        }
        if (!message.payload.retryable) this.syncRequired = true;
        if (message.payload.code === 'sequence_gap') {
          await this.handleSyncRequired({
            connection_id: this.session.connection_id,
            creator_account_id: this.session.creator_account_id,
            reason: 'sequence_gap',
            expected_agent_stream_id: this.identity.agentStreamId,
            expected_next_source_seq: this.identity.lastAcknowledgedSourceSeq + 1,
            pending_snapshot_id: null,
            next_expected_chunk_index: 0,
            snapshot: {
              include_chats: true,
              include_messages: true,
              include_coverage_evidence: true,
              max_frame_bytes: 524_288,
              max_records_per_chunk: 100,
            },
          });
        }
        return;
      case 'config.available':
        this.onConfigAvailable(message.payload);
        if (this.configClient !== null) {
          await this.configClient.requireConfig(message.payload, { force: true });
        }
        return;
      case 'command.execute':
        await this.handleCommand(message);
        return;
      case 'command.result.ack':
        const compaction = this.commandService.acknowledge(message.payload);
        this.onCommandResultAcknowledged(message.payload);
        await compaction;
        return;
      default:
        return;
    }
  }

  async handleCommand(message) {
    const result = await this.commandService.execute(
      message.payload,
      this.session,
      message.message_id,
    );
    this.sendCommandResult(result, message.message_id);
  }

  async resendCommandResults() {
    const pending = await this.commandService.pendingResults(
      this.session?.creator_account_id ?? null,
    );
    for (const record of pending) {
      this.sendCommandResult(record.result, record.correlation_id);
    }
  }

  startHeartbeat(delay) {
    this.clearHeartbeat();
    this.heartbeatIntervalMs = delay;
    this.heartbeatTimer = this.scheduler.setInterval(() => this.sendHeartbeatIfDue(), delay);
  }

  forceReconnect(reason = 'Agent session must be renewed') {
    this.reconnectAllowed = true;
    const socket = this.socket;
    if (socket === null) {
      this.ensureConnected();
      return;
    }
    try {
      socket.close(LEASE_EXPIRED_CLOSE_CODE, reason);
    } catch (_error) {
      this.handleClose(socket);
    }
  }

  handleClose(socket) {
    if (this.socket !== socket) return;
    this.socket = null;
    this.session = null;
    this.configClient?.clearSessionAuthorization?.();
    this.onSessionLost({ reason: 'disconnected' });
    this.clearHeartbeat();
    if (!this.stopped && this.reconnectAllowed) this.scheduleReconnect();
  }

  scheduleReconnect() {
    if (this.reconnectTimer !== null) return;
    const exponential = Math.min(
      this.reconnectMaxMs,
      this.reconnectBaseMs * 2 ** this.reconnectAttempt,
    );
    const delay = Math.max(0, Math.round(exponential * (0.5 + this.random())));
    this.reconnectAttempt += 1;
    this.reconnectTimer = this.scheduler.setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.stopped) this.openSocket();
    }, delay);
  }

  clearReconnect() {
    if (this.reconnectTimer !== null) this.scheduler.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  clearHeartbeat() {
    if (this.heartbeatTimer !== null) this.scheduler.clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
    this.heartbeatIntervalMs = null;
    this.lastHeartbeatSentAt = null;
  }
}
