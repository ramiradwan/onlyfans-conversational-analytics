import type {
  AgentStatePayload,
  AnalyticsView,
  BridgeSessionPayload,
  ConversationCoverage,
  ConversationRecord,
  ConversationSummary,
  HistoricalCoverage,
  LiveFreshness,
  MessagePageResponse,
  MessageView,
  PresenceStatePayload,
  ProjectionState,
  ProtocolErrorPayload,
  StateDeltaPayload,
  StateSnapshotPayload,
  SystemStatePayload,
} from '../protocol';

export type BridgeConnectionState =
  | 'idle'
  | 'connecting'
  | 'handshaking'
  | 'connected'
  | 'reconnecting'
  | 'disconnected'
  | 'error';

export type ReadModelState = 'empty' | 'loading' | 'realtime' | 'resyncing' | 'degraded';
export type MessagePageStatus = 'idle' | 'loading' | 'ready' | 'error';

export const DEFAULT_MESSAGE_PAGE_SIZE = 50;
export const MAX_ACTIVE_MESSAGE_PAGES = 20;
export const MAX_INACTIVE_MESSAGE_PAGES = 2;
export const MAX_INACTIVE_CACHED_CONVERSATIONS = 5;
export const MAX_CACHED_CONVERSATIONS = MAX_INACTIVE_CACHED_CONVERSATIONS + 1;
export const MAX_CACHED_MESSAGES_PER_CONVERSATION =
  DEFAULT_MESSAGE_PAGE_SIZE * MAX_ACTIVE_MESSAGE_PAGES;

export interface SnapshotProgress {
  phase: HistoricalCoverage['phase'];
  discoveredConversations: number | null;
  completeConversations: number;
  partialConversations: number;
  percentage: number | null;
}

export interface ConversationMessageState {
  items: MessageView[];
  olderCursor: string | null;
  hasOlderStoredItems: boolean;
  hasNewerUncachedItems: boolean;
  conversationCoverage: ConversationCoverage;
  projection: ProjectionState | null;
  projectionGeneration: string | null;
  readRevision: number | null;
  generatedAt: string | null;
  status: MessagePageStatus;
  error: string | null;
}

export interface BridgeTransportState {
  connection: BridgeConnectionState;
  creatorAccountId: string | null;
  session: BridgeSessionPayload | null;
  readModelState: ReadModelState;
  viewRevision: number | null;
  conversations: ConversationSummary[];
  analytics: AnalyticsView | null;
  coverage: HistoricalCoverage;
  projection: ProjectionState;
  liveFreshness: LiveFreshness;
  snapshotProgress: SnapshotProgress;
  messagePages: Readonly<Record<string, ConversationMessageState>>;
  presence: PresenceStatePayload | null;
  agent: AgentStatePayload | null;
  system: SystemStatePayload | null;
  protocolError: ProtocolErrorPayload | null;
}

export interface BridgeTransportStore {
  getState(): Readonly<BridgeTransportState>;
  subscribe(listener: () => void): () => void;
  bindAccount(accountId: string): void;
  setConnection(connection: BridgeConnectionState): void;
  acceptSession(session: BridgeSessionPayload): void;
  applySnapshot(snapshot: StateSnapshotPayload): boolean;
  applyDelta(delta: StateDeltaPayload): 'applied' | 'duplicate' | 'gap' | 'invalid';
  beginResync(): void;
  setActiveConversation(conversationId: string | null): void;
  beginMessagePage(conversationId: string, reset?: boolean): number;
  applyMessagePage(
    page: MessagePageResponse,
    mode?: 'replace' | 'prepend',
    expectedProjectionEpoch?: number,
  ): 'applied' | 'stale' | 'invalid';
  failMessagePage(conversationId: string, detail: string): void;
  clearMessagePage(conversationId: string): void;
  clearMessageCache(): void;
  setPresence(presence: PresenceStatePayload): void;
  expirePresence(expectedExpiresAt: string): void;
  setAgent(agent: AgentStatePayload): void;
  setSystem(system: SystemStatePayload): void;
  setProtocolError(error: ProtocolErrorPayload): void;
  markDisconnected(): void;
  reset(): void;
}

export const UNKNOWN_COVERAGE: HistoricalCoverage = {
  status: 'unknown',
  phase: 'not_started',
  generation_id: null,
  as_of: null,
  discovered_conversations: null,
  complete_conversations: 0,
  complete_as_of: null,
  reason: null,
};

export const PENDING_PROJECTION: ProjectionState = {
  status: 'pending',
  canonical_revision: 0,
  projected_revision: 0,
  projected_at: null,
  reason: null,
};

export const UNKNOWN_LIVE_FRESHNESS: LiveFreshness = {
  status: 'unknown',
  last_observed_at: null,
  last_committed_at: null,
  expires_at: null,
  pending_count: null,
  reason: null,
};

export function isConversationSummary(
  conversation: ConversationRecord,
): conversation is ConversationSummary {
  return true;
}

export function conversationLatestMessage(
  conversation: ConversationRecord,
): MessageView | null {
  return conversation.latest_message;
}

function cloneCoverage(coverage: ConversationCoverage): ConversationCoverage {
  return { ...coverage };
}

function cloneAnalytics(analytics: AnalyticsView): AnalyticsView {
  return Object.fromEntries(
    Object.entries(analytics).map(([name, metric]) => [
      name,
      {
        ...metric,
        observed_range: { ...metric.observed_range },
        complete_range: metric.complete_range ? { ...metric.complete_range } : null,
      },
    ]),
  ) as unknown as AnalyticsView;
}

function cloneConversation(conversation: ConversationRecord): ConversationRecord {
  return {
    ...conversation,
    coverage: cloneCoverage(conversation.coverage),
    latest_message: conversation.latest_message ? { ...conversation.latest_message } : null,
  };
}

function validConversations(conversations: readonly ConversationRecord[]): boolean {
  const conversationIds = new Set<string>();
  for (const conversation of conversations) {
    if (conversationIds.has(conversation.conversation_id)) return false;
    conversationIds.add(conversation.conversation_id);
  }
  return true;
}

function progress(coverage: HistoricalCoverage): SnapshotProgress {
  const discovered = coverage.discovered_conversations;
  const percentage =
    discovered !== null && discovered > 0
      ? Math.min(100, Math.round((coverage.complete_conversations / discovered) * 100))
      : coverage.status === 'complete'
        ? 100
        : null;
  return {
    phase: coverage.phase,
    discoveredConversations: discovered,
    completeConversations: coverage.complete_conversations,
    partialConversations:
      coverage.discovered_conversations === null
        ? 0
        : Math.max(0, coverage.discovered_conversations - coverage.complete_conversations),
    percentage,
  };
}

function unknownConversationCoverage(): ConversationCoverage {
  return {
    status: 'unknown',
    boundary: null,
    earliest_available_at: null,
    latest_acquired_at: null,
    data_as_of: null,
    reason_code: 'message_page_not_loaded',
  };
}

function emptyMessagePage(coverage = unknownConversationCoverage()): ConversationMessageState {
  return {
    items: [],
    olderCursor: null,
    hasOlderStoredItems: false,
    hasNewerUncachedItems: false,
    conversationCoverage: cloneCoverage(coverage),
    projection: null,
    projectionGeneration: null,
    readRevision: null,
    generatedAt: null,
    status: 'idle',
    error: null,
  };
}

function initialState(): BridgeTransportState {
  const coverage = { ...UNKNOWN_COVERAGE };
  return {
    connection: 'idle',
    creatorAccountId: null,
    session: null,
    readModelState: 'empty',
    viewRevision: null,
    conversations: [],
    analytics: null,
    coverage,
    projection: { ...PENDING_PROJECTION },
    liveFreshness: { ...UNKNOWN_LIVE_FRESHNESS },
    snapshotProgress: progress(coverage),
    messagePages: {},
    presence: null,
    agent: null,
    system: null,
    protocolError: null,
  };
}

function sortMessages(messages: MessageView[]): MessageView[] {
  return messages.sort(
    (left, right) =>
      Date.parse(left.sent_at) - Date.parse(right.sent_at) ||
      left.message_id.localeCompare(right.message_id),
  );
}

export function createBridgeTransportStore(): BridgeTransportStore {
  let state = initialState();
  let messageCacheOrder: string[] = [];
  let activeConversationId: string | null = null;
  let messageProjectionEpoch = 0;
  const listeners = new Set<() => void>();

  const publish = (patch: Partial<BridgeTransportState>) => {
    state = { ...state, ...patch };
    listeners.forEach((listener) => listener());
  };

  const assertAccount = (accountId: string) => {
    if (state.creatorAccountId !== accountId) {
      throw new Error('Message account conflicts with the active Bridge binding');
    }
  };

  const messageLimit = (conversationId: string) =>
    DEFAULT_MESSAGE_PAGE_SIZE *
    (conversationId === activeConversationId
      ? MAX_ACTIVE_MESSAGE_PAGES
      : MAX_INACTIVE_MESSAGE_PAGES);

  const trimPage = (
    conversationId: string,
    page: ConversationMessageState,
  ): ConversationMessageState => {
    const limit = messageLimit(conversationId);
    if (page.items.length <= limit) return page;
    return {
      ...page,
      items: page.items.slice(-limit),
      hasOlderStoredItems: true,
    };
  };

  const publishMessagePage = (conversationId: string, page: ConversationMessageState) => {
    messageCacheOrder = messageCacheOrder.filter((id) => id !== conversationId);
    messageCacheOrder.push(conversationId);
    const pages = { ...state.messagePages, [conversationId]: trimPage(conversationId, page) };
    const inactiveIds = () =>
      messageCacheOrder.filter((id) => id !== activeConversationId);
    while (inactiveIds().length > MAX_INACTIVE_CACHED_CONVERSATIONS) {
      const evicted = inactiveIds()[0];
      if (evicted !== undefined) delete pages[evicted];
      messageCacheOrder = messageCacheOrder.filter((id) => id !== evicted);
    }
    publish({ messagePages: pages });
  };

  return {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    bindAccount(accountId) {
      if (state.creatorAccountId !== accountId) {
        state = initialState();
        messageCacheOrder = [];
        activeConversationId = null;
        messageProjectionEpoch += 1;
      }
      publish({
        creatorAccountId: accountId,
        connection: 'connecting',
        readModelState: 'loading',
      });
    },
    setConnection(connection) {
      publish({ connection });
    },
    acceptSession(session) {
      assertAccount(session.creator_account_id);
      publish({
        session,
        connection: 'connected',
        readModelState: 'loading',
        protocolError: null,
      });
    },
    applySnapshot(snapshot) {
      assertAccount(snapshot.creator_account_id);
      if (!validConversations(snapshot.conversations)) return false;

      const coverage = { ...snapshot.coverage };
      const projection = { ...snapshot.projection };
      const liveFreshness = { ...snapshot.live_freshness };
      const conversations = snapshot.conversations.map(cloneConversation);
      const conversationIds = new Set(conversations.map(({ conversation_id }) => conversation_id));
      const messagePages: Record<string, ConversationMessageState> = {};
      const sameProjectionActivation =
        state.viewRevision === snapshot.view_revision &&
        state.projection.projected_revision === projection.projected_revision;

      if (!sameProjectionActivation) messageProjectionEpoch += 1;

      for (const [conversationId, page] of Object.entries(state.messagePages)) {
        if (
          sameProjectionActivation &&
          conversationIds.has(conversationId) &&
          page.projection?.projected_revision === projection.projected_revision
        ) {
          messagePages[conversationId] = page;
        }
      }
      messageCacheOrder = messageCacheOrder.filter((id) => id in messagePages);

      publish({
        conversations,
        analytics: cloneAnalytics(snapshot.analytics),
        coverage,
        projection,
        liveFreshness,
        snapshotProgress: progress(coverage),
        messagePages,
        viewRevision: snapshot.view_revision,
        readModelState: 'realtime',
      });
      return true;
    },
    applyDelta(delta) {
      assertAccount(delta.creator_account_id);
      const currentRevision = state.viewRevision;
      if (currentRevision !== null && delta.view_revision <= currentRevision) return 'duplicate';
      if (
        currentRevision === null ||
        state.readModelState === 'resyncing' ||
        delta.view_revision !== currentRevision + 1
      ) return 'gap';

      const conversations = state.conversations.map(cloneConversation);
      let analytics = state.analytics ? cloneAnalytics(state.analytics) : null;
      let coverage = { ...state.coverage };
      let projection = { ...state.projection };
      let liveFreshness = { ...state.liveFreshness };
      const messagePages = { ...state.messagePages };
      const changedKeys = new Set<string>();
      let projectionActivationChanged = false;

      for (const change of delta.changes) {
        if (change.type === 'analytics.replace') {
          if (changedKeys.has('analytics')) return 'invalid';
          changedKeys.add('analytics');
          analytics = cloneAnalytics(change.analytics);
          continue;
        }
        if (change.type === 'coverage.replace') {
          if (changedKeys.has('coverage')) return 'invalid';
          changedKeys.add('coverage');
          coverage = { ...change.coverage };
          continue;
        }
        if (change.type === 'projection.replace') {
          if (changedKeys.has('projection')) return 'invalid';
          changedKeys.add('projection');
          projectionActivationChanged =
            projection.projected_revision !== change.projection.projected_revision ||
            projection.projected_at !== change.projection.projected_at;
          projection = { ...change.projection };
          continue;
        }
        if (change.type === 'live_freshness.replace') {
          if (changedKeys.has('live_freshness')) return 'invalid';
          changedKeys.add('live_freshness');
          liveFreshness = { ...change.live_freshness };
          continue;
        }

        const conversationId =
          change.type === 'conversation.upsert'
            ? change.conversation.conversation_id
            : change.conversation_id;

        const index = conversations.findIndex(
          (conversation) => conversation.conversation_id === conversationId,
        );
        if (change.type === 'conversation.delete') {
          const key = `conversation:${conversationId}`;
          if (changedKeys.has(key) || index === -1) return 'invalid';
          changedKeys.add(key);
          conversations.splice(index, 1);
          delete messagePages[conversationId];
          messageCacheOrder = messageCacheOrder.filter((id) => id !== conversationId);
          continue;
        }

        if (change.type === 'conversation.upsert') {
          const key = `conversation:${conversationId}`;
          if (changedKeys.has(key) || !validConversations([change.conversation])) {
            return 'invalid';
          }
          changedKeys.add(key);
          const replacement = cloneConversation(change.conversation);
          if (index === -1) conversations.push(replacement);
          else conversations[index] = replacement;
          continue;
        }

        if (change.type === 'conversation.coverage.replace') {
          const key = `conversation-coverage:${conversationId}`;
          if (changedKeys.has(key) || index === -1) return 'invalid';
          changedKeys.add(key);
          conversations[index] = {
            ...conversations[index],
            coverage: cloneCoverage(change.coverage),
          };
          const page = messagePages[conversationId];
          if (page) {
            messagePages[conversationId] = {
              ...page,
              conversationCoverage: cloneCoverage(change.coverage),
            };
          }
          continue;
        }

        const messageId =
          change.type === 'message.tail.delete' ? change.message_id : change.message.message_id;
        const key = `message-tail:${conversationId}:${messageId}`;
        if (changedKeys.has(key)) return 'invalid';
        changedKeys.add(key);
        const page = messagePages[conversationId];
        if (!page) continue;
        if (change.type === 'message.tail.delete') {
          messagePages[conversationId] = {
            ...page,
            items: page.items.filter(({ message_id }) => message_id !== change.message_id),
          };
          continue;
        }

        const prior = page.items.find(({ message_id }) => message_id === change.message.message_id);
        if (prior && JSON.stringify(prior) !== JSON.stringify(change.message)) return 'invalid';
        const tail = sortMessages([
          ...page.items.filter(({ message_id }) => message_id !== change.message.message_id),
          { ...change.message },
        ]);
        const limit = messageLimit(conversationId);
        messagePages[conversationId] = {
          ...page,
          items: tail.slice(-limit),
          hasOlderStoredItems: page.hasOlderStoredItems || tail.length > limit,
        };
      }
      if (!validConversations(conversations)) return 'invalid';
      if (projectionActivationChanged) {
        messageProjectionEpoch += 1;
        for (const conversationId of Object.keys(messagePages)) delete messagePages[conversationId];
        messageCacheOrder = [];
      }
      publish({
        conversations,
        analytics,
        coverage,
        projection,
        liveFreshness,
        snapshotProgress: progress(coverage),
        messagePages,
        viewRevision: delta.view_revision,
        readModelState: 'realtime',
      });
      return 'applied';
    },
    beginResync() {
      messageProjectionEpoch += 1;
      publish({ readModelState: 'resyncing' });
    },
    setActiveConversation(conversationId) {
      activeConversationId = conversationId;
      const pages = { ...state.messagePages };
      for (const [id, page] of Object.entries(pages)) pages[id] = trimPage(id, page);
      publish({ messagePages: pages });
    },
    beginMessagePage(conversationId, reset = false) {
      const conversation = state.conversations.find(
        (item) => item.conversation_id === conversationId,
      );
      const current = reset
        ? emptyMessagePage(
            conversation?.coverage,
          )
        : state.messagePages[conversationId] ??
          emptyMessagePage(
            conversation?.coverage,
          );
      publishMessagePage(conversationId, { ...current, status: 'loading', error: null });
      return messageProjectionEpoch;
    },
    applyMessagePage(page, mode = 'replace', expectedProjectionEpoch = undefined) {
      assertAccount(page.creator_account_id);
      if (!state.conversations.some(({ conversation_id }) => conversation_id === page.conversation_id)) {
        return 'invalid';
      }
      if (
        (expectedProjectionEpoch !== undefined &&
          expectedProjectionEpoch !== messageProjectionEpoch) ||
        page.projection.projected_revision !== state.projection.projected_revision
      ) {
        return 'stale';
      }
      const identifiers = new Set<string>();
      for (const item of page.items) {
        if (identifiers.has(item.message_id)) return 'invalid';
        identifiers.add(item.message_id);
      }

      const current = state.messagePages[page.conversation_id];
      if (
        mode === 'prepend' &&
        current?.projectionGeneration !== null &&
        current?.projectionGeneration !== undefined &&
        (current.projectionGeneration !== page.projection_generation ||
          current.readRevision !== page.read_revision)
      ) {
        return 'stale';
      }

      const merged = new Map<string, MessageView>();
      if (mode === 'prepend') {
        for (const item of current?.items ?? []) merged.set(item.message_id, { ...item });
      }
      for (const item of page.items) merged.set(item.message_id, { ...item });
      let items = sortMessages([...merged.values()]);
      let hasNewerUncachedItems = current?.hasNewerUncachedItems ?? false;
      const limit = messageLimit(page.conversation_id);
      if (items.length > limit) {
        if (mode === 'prepend') {
          items = items.slice(0, limit);
          hasNewerUncachedItems = true;
        } else {
          items = items.slice(-limit);
        }
      }

      publishMessagePage(page.conversation_id, {
        items,
        olderCursor: page.older_cursor,
        hasOlderStoredItems: page.has_older_stored_items,
        hasNewerUncachedItems,
        conversationCoverage: { ...page.conversation_coverage },
        projection: { ...page.projection },
        projectionGeneration: page.projection_generation,
        readRevision: page.read_revision,
        generatedAt: page.generated_at,
        status: 'ready',
        error: null,
      });
      return 'applied';
    },
    failMessagePage(conversationId, detail) {
      const current = state.messagePages[conversationId] ?? emptyMessagePage();
      publishMessagePage(conversationId, { ...current, status: 'error', error: detail });
    },
    clearMessagePage(conversationId) {
      messageCacheOrder = messageCacheOrder.filter((id) => id !== conversationId);
      const pages = { ...state.messagePages };
      delete pages[conversationId];
      publish({ messagePages: pages });
    },
    clearMessageCache() {
      messageCacheOrder = [];
      publish({ messagePages: {} });
    },
    setPresence(presence) {
      assertAccount(presence.creator_account_id);
      publish({ presence: { ...presence } });
    },
    expirePresence(expectedExpiresAt) {
      if (
        state.presence?.freshness === 'current' &&
        state.presence.expires_at === expectedExpiresAt
      ) {
        publish({
          presence: {
            ...state.presence,
            freshness: 'unknown',
            online_platform_user_ids: [],
          },
        });
      }
    },
    setAgent(agent) {
      assertAccount(agent.creator_account_id);
      publish({ agent: { ...agent } });
    },
    setSystem(system) {
      assertAccount(system.creator_account_id);
      publish({ system: { ...system } });
    },
    setProtocolError(error) {
      publish({
        protocolError: { ...error },
        connection: error.fatal ? 'error' : state.connection,
      });
    },
    markDisconnected() {
      publish({
        connection: 'disconnected',
        session: null,
        readModelState: state.viewRevision === null ? 'empty' : 'degraded',
        liveFreshness: {
          ...state.liveFreshness,
          status: state.liveFreshness.status === 'current' ? 'delayed' : state.liveFreshness.status,
          reason: 'bridge_disconnected',
        },
        system: state.system
          ? {
              ...state.system,
              readiness: 'degraded',
              detail: 'Bridge connection to Brain is unavailable',
            }
          : null,
      });
    },
    reset() {
      state = initialState();
      messageCacheOrder = [];
      activeConversationId = null;
      messageProjectionEpoch += 1;
      listeners.forEach((listener) => listener());
    },
  };
}

export const bridgeTransportStore = createBridgeTransportStore();
